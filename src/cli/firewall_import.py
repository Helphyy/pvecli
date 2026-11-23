"""`cluster firewall import`: make the firewall match a file.

The file is what `cluster firewall export` writes. Each level the file
names (the datacenter, a node, a guest, a vnet) becomes exactly what the
file says: its options, aliases, IP sets, security groups and rules. A
level the file leaves out is not touched.

The writes go in an order that never leaves a reference broken: objects
added or changed first, then the rules, then the objects removed. Rules
are edited in place: the ones the file drops go (last first), the new ones
come in at their position, disabled until they are there. The guard of
the access to the API judges the firewall the file leaves.
"""

from __future__ import annotations

import copy
import ipaddress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

import typer
import yaml

from ..api.client import ProxmoxClient, is_not_implemented
from ..api.exceptions import PVECliError
from ..config import ConfigManager
from ..utils import console, print_error, print_info, print_success
from ..utils.helpers import async_to_sync
from .firewall import (
    _RULE_META,
    Scope,
    _guests,
    _vnets,
    cluster_scope,
    describe_rule,
    effective,
    group_scope,
    guest_scope,
    is_on,
    node_scope,
    read_rules,
    vnet_scope,
)
from .firewall_edit import OPTION_TYPES, Writer, approved, insert_rule, require_write

_RULE_KEYS = {"type", "action", "macro", "proto", "source", "dest", "sport", "dport", "icmp-type",
              "iface", "log", "comment", "enable"}
_LEVEL_KEYS = {
    "datacenter": {"options", "aliases", "ipsets", "groups", "rules"},
    "node": {"options", "rules"},
    "guest": {"type", "name", "options", "rules", "aliases", "ipsets"},
    "vnet": {"options", "rules"},
}


def fail(message: str) -> NoReturn:
    print_error(message)
    raise typer.Exit(1)


# ── the file ─────────────────────────────────────────────────────────────


def load(path: str) -> dict[str, Any]:
    """Read and check the shape of a firewall file (YAML or JSON)."""
    p = Path(path)
    if not p.exists():
        fail(f"File not found: {path}")
    try:
        doc = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        fail(f"{path} is not valid YAML: {e}")
    if not isinstance(doc, dict) or doc.get("pvecli_firewall") != 1:
        fail(f"{path} is not a firewall file: it must start with 'pvecli_firewall: 1' "
             "(see cluster firewall export)")
    unknown = set(doc) - {"pvecli_firewall", "datacenter", "nodes", "guests", "vnets"}
    if unknown:
        fail(f"Unknown key(s) at the top of {path}: {', '.join(sorted(unknown))}")
    if "datacenter" in doc:
        _check_level("datacenter", "datacenter", doc["datacenter"])
    for section, kind in (("nodes", "node"), ("guests", "guest"), ("vnets", "vnet")):
        levels = doc.get(section) or {}
        if not isinstance(levels, dict):
            fail(f"'{section}' must map each {kind} to its firewall")
        for name, level in levels.items():
            _check_level(kind, f"{kind} {name}", level)
            if kind == "guest" and not str(name).isdigit():
                fail(f"guests: '{name}' is not a VM or container ID")
    return doc


def _check_level(kind: str, label: str, level: Any) -> None:
    if level is None:
        return
    if not isinstance(level, dict):
        fail(f"{label}: must be a mapping")
    unknown = set(level) - _LEVEL_KEYS[kind]
    if unknown:
        fail(f"{label}: unknown key(s) {', '.join(sorted(unknown))}")
    opts = level.get("options") or {}
    if not isinstance(opts, dict):
        fail(f"{label}: options must be a mapping")
    allowed = OPTION_TYPES["cluster" if kind == "datacenter" else "vm" if kind == "guest" else kind]
    bad = set(opts) - set(allowed)
    if bad:
        fail(f"{label}: unknown option(s) {', '.join(sorted(bad))}")
    for i, rule in enumerate(level.get("rules") or []):
        _check_rule(f"{label}, rule {i}", rule)
    for g in level.get("groups") or []:
        if not isinstance(g, dict) or "name" not in g:
            fail(f"{label}: each group needs a name")
        for i, rule in enumerate(g.get("rules") or []):
            _check_rule(f"security group {g['name']}, rule {i}", rule)
    for a in level.get("aliases") or []:
        if not isinstance(a, dict) or not a.get("name") or not a.get("cidr"):
            fail(f"{label}: each alias needs a name and a cidr")
    for s in level.get("ipsets") or []:
        if not isinstance(s, dict) or not s.get("name"):
            fail(f"{label}: each IP set needs a name")
        for e in s.get("entries") or []:
            if not isinstance(e, dict) or not e.get("cidr"):
                fail(f"{label}, IP set {s['name']}: each entry needs a cidr")


def _check_rule(label: str, rule: Any) -> None:
    if not isinstance(rule, dict) or "type" not in rule or "action" not in rule:
        fail(f"{label}: a rule needs a type and an action")
    unknown = set(rule) - _RULE_KEYS - set(_RULE_META)
    if unknown:
        fail(f"{label}: unknown key(s) {', '.join(sorted(unknown))}")


def _flag(value: Any) -> str:
    """A yes/no as Proxmox stores it: YAML writes true, yes, on; the API 1."""
    return "1" if str(value).lower() in ("1", "true", "yes", "on") else "0"


def norm_value(key: str, value: Any, kinds: dict[str, Any]) -> str:
    return _flag(value) if kinds.get(key) is bool else str(value)


def norm_rule(rule: dict[str, Any]) -> dict[str, Any]:
    """A rule as compared: configuration only, enable explicit, values as text.

    `log: nolog` is no log at all, as when the key is left out.
    """
    out = {k: str(v) for k, v in rule.items()
           if k in _RULE_KEYS and v not in (None, "") and not (k == "log" and v == "nolog")}
    out["enable"] = _flag(rule.get("enable", 1))
    return out


def _key(rule: dict[str, Any]) -> tuple:
    return tuple(sorted(norm_rule(rule).items()))


def _names(token: str) -> tuple[str, str] | None:
    """('ipset' or 'alias', name) when a source or dest token names an object."""
    token = token.strip()
    if token.startswith("+"):
        return "ipset", token[1:]
    try:
        ipaddress.ip_network(token, strict=False)
        return None
    except ValueError:
        pass
    if "-" in token and all(_is_ip(t) for t in token.split("-", 1)):
        return None
    return "alias", token


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def check_refs(doc: dict[str, Any], dc_now: dict[str, set[str]]) -> None:
    """Every object the file names exists once it is imported.

    Rules name aliases and IP sets (source, dest) and security groups (a
    group rule); IP set entries name aliases. dc_now: the aliases, IP sets
    and groups of the datacenter as they are, when the file leaves it out.
    """
    dc = doc.get("datacenter")
    if dc is not None:
        dc_objects = {"alias": {a["name"] for a in (dc or {}).get("aliases") or []},
                      "ipset": {s["name"] for s in (dc or {}).get("ipsets") or []},
                      "group": {g["name"] for g in (dc or {}).get("groups") or []}}
    else:
        dc_objects = dc_now
    problems = []

    def exists(what: str, name: str, own: dict[str, set[str]] | None) -> bool:
        if name.startswith("dc/"):
            return name[3:] in dc_objects[what]
        if name.startswith("guest/"):
            return own is not None and name[6:] in own[what]
        return name in dc_objects[what] or (own is not None and name in own[what])

    def check(label: str, rules: Any, own: dict[str, set[str]] | None) -> None:
        for i, r in enumerate(rules or []):
            if r.get("type") == "group":
                if r.get("action") not in dc_objects["group"]:
                    problems.append(f"{label}, rule {i}: no security group '{r.get('action')}'")
                continue
            for fld in ("source", "dest"):
                for token in str(r.get(fld) or "").split(","):
                    named = _names(token) if token.strip() else None
                    if named and not exists(named[0], named[1], own):
                        problems.append(f"{label}, rule {i} ({fld}): no {'IP set' if named[0] == 'ipset' else 'alias'} "
                                        f"'{named[1].split('/')[-1]}'")

    def check_sets(label: str, sets: Any, own: dict[str, set[str]] | None) -> None:
        for st in sets or []:
            for e in st.get("entries") or []:
                named = _names(str(e.get("cidr", "")))
                if named and named[0] == "alias" and not exists("alias", named[1], own):
                    problems.append(f"{label}, IP set {st['name']}: no alias '{named[1].split('/')[-1]}'")

    if dc is not None:
        check("datacenter", (dc or {}).get("rules"), None)
        check_sets("datacenter", (dc or {}).get("ipsets"), None)
        for g in (dc or {}).get("groups") or []:
            check(f"security group {g['name']}", g.get("rules"), None)
    for name, level in (doc.get("nodes") or {}).items():
        check(f"node {name}", (level or {}).get("rules"), None)
    for vmid, level in (doc.get("guests") or {}).items():
        level = level or {}
        own = {"alias": {a["name"] for a in level.get("aliases") or []},
               "ipset": {s["name"] for s in level.get("ipsets") or []}, "group": set()}
        check(f"guest {vmid}", level.get("rules"), own)
        check_sets(f"guest {vmid}", level.get("ipsets"), own)
    for name, level in (doc.get("vnets") or {}).items():
        check(f"vnet {name}", (level or {}).get("rules"), None)
    if problems:
        fail("The file names objects that would not exist:\n  " + "\n  ".join(problems))


async def used_outside(client: ProxmoxClient, doc: dict[str, Any], removed: dict[str, list[str]],
                       force: bool) -> None:
    """Refuse removing datacenter objects that levels the file leaves out still use.

    The levels the file names are checked by check_refs: they become what
    the file says. Every other level keeps its rules, and loses what they name.
    """
    from .firewall_edit import find_refs, refuse_if_used

    in_file = {"cluster", "group"}
    nodes = set((doc.get("nodes") or {}).keys())
    guests = {int(v) for v in (doc.get("guests") or {}).keys()}
    vnets = set((doc.get("vnets") or {}).keys())

    def outside(scope: Scope) -> bool:
        if scope.kind in in_file:
            return False
        if scope.kind == "node":
            return scope.name not in nodes
        if scope.is_guest:
            return scope.vmid not in guests
        return scope.name not in vnets

    for obj, names in removed.items():
        for name in names:
            refs = [r for r in await find_refs(client, obj, name, cluster_scope()) if outside(r.scope)]
            what = {"alias": "Alias", "ipset": "IP set", "group": "Security group"}[obj]
            refuse_if_used(f"{what} '{name}', which the file removes from the datacenter,", refs, force)


# ── the plan ─────────────────────────────────────────────────────────────


@dataclass
class Step:
    # The order of the writes: a switch turned off first (-1), objects added
    # or changed (0), rules (1), then what nothing uses any more, users
    # before what they use: groups (2), IP set entries and sets (3), aliases
    # (4), and a switch turned on or other options last (5)
    phase: int
    text: str
    run: Any  # async (Writer) -> None


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)
    seen: dict[str, Any] = field(default_factory=dict)  # lists read, for their digests
    scopes: list[Scope] = field(default_factory=list)

    def add(self, phase: int, text: str, run: Any) -> None:
        self.steps.append(Step(phase, text, run))


def rule_edits(current: list[dict[str, Any]], target: list[dict[str, Any]]) -> tuple[list[int], list[tuple[int, dict[str, Any]]]]:
    """(positions to remove, (position, rule) to insert) turning current into target.

    The rules both lists share, in the same order, stay where they are (a
    longest common subsequence): only the others are written.
    """
    a, b = [_key(r) for r in current], [_key(r) for r in target]
    n, m = len(a), len(b)
    lcs = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            lcs[i][j] = lcs[i + 1][j + 1] + 1 if a[i] == b[j] else max(lcs[i + 1][j], lcs[i][j + 1])
    keep_a, keep_b = set(), set()
    i = j = 0
    while i < n and j < m:
        if a[i] == b[j]:
            keep_a.add(i)
            keep_b.add(j)
            i, j = i + 1, j + 1
        elif lcs[i + 1][j] >= lcs[i][j + 1]:
            i += 1
        else:
            j += 1
    removed = [i for i in range(n) if i not in keep_a]
    inserted = [(j, target[j]) for j in range(m) if j not in keep_b]
    return removed, inserted


def _plan_rules(plan: Plan, scope: Scope, current: list[dict[str, Any]], target: list[dict[str, Any]]) -> None:
    plan.seen[scope.rules_path] = current
    removed, inserted = rule_edits(current, target)
    if not removed and not inserted:
        return
    for pos in sorted(removed, reverse=True):
        plan.add(1, f"{scope.label}: remove rule {pos}: {describe_rule(current[pos])}",
                 _remove_rule(scope, pos))
    for pos, rule in inserted:
        plan.add(1, f"{scope.label}: add rule at {pos}: {describe_rule(rule)}", _add_rule(scope, pos, rule))


def _remove_rule(scope: Scope, pos: int) -> Any:
    async def run(w: Writer) -> None:
        await w.delete(f"{scope.rules_path}/{pos}")
    return run


def _add_rule(scope: Scope, pos: int, rule: dict[str, Any]) -> Any:
    data = {k: v for k, v in rule.items() if k in _RULE_KEYS}
    data["enable"] = 1 if str(rule.get("enable", 1)) in ("1", "True", "true") else 0

    async def run(w: Writer) -> None:
        await insert_rule(w, scope, data, pos)
    return run


def _plan_options(plan: Plan, scope: Scope, current: dict[str, Any], target: dict[str, Any]) -> None:
    plan.seen[scope.options_path] = current
    clean = {k: v for k, v in current.items() if k != "digest"}
    kinds = OPTION_TYPES[scope.kind]
    target = {k: (int(_flag(v)) if kinds.get(k) is bool else v) for k, v in target.items()}
    data: dict[str, Any] = {k: v for k, v in target.items()
                            if k not in clean or norm_value(k, clean[k], kinds) != norm_value(k, v, kinds)}
    gone = [k for k in clean if k not in target]
    if gone:
        data["delete"] = ",".join(sorted(gone))
    if not data:
        return
    text = ", ".join([f"{k}={v}" for k, v in data.items() if k != "delete"]
                     + [f"{k} back to default" for k in gone])
    # Turned off first, so nothing half-undone is ever enforced; turned on
    # last, once its rules and objects are in place
    was_on = is_on(effective(scope.kind, clean, "enable"))
    will_be_on = is_on(effective(scope.kind, target, "enable"))
    turned_off = was_on and not will_be_on
    plan.add(-1 if turned_off else 5, f"{scope.label}: options {text}", _put(scope.options_path, data))


def _put(path: str, data: dict[str, Any]) -> Any:
    async def run(w: Writer) -> None:
        await w.put(path, dict(data))
    return run


def _post(path: str, data: dict[str, Any]) -> Any:
    async def run(w: Writer) -> None:
        await w.post(path, dict(data))
    return run


def _delete(path: str, params: dict[str, Any] | None = None) -> Any:
    async def run(w: Writer) -> None:
        await w.delete(path, params)
    return run


async def _plan_objects(client: ProxmoxClient, plan: Plan, scope: Scope, level: dict[str, Any]) -> None:
    base = scope.base
    aliases = await client.get(f"{base}/aliases") or []
    plan.seen[f"{base}/aliases"] = aliases
    have = {a["name"]: a for a in aliases}
    want = {a["name"]: a for a in level.get("aliases") or []}
    for name, a in want.items():
        data = {"cidr": a["cidr"], **({"comment": a["comment"]} if a.get("comment") else {})}
        if name not in have:
            plan.add(0, f"{scope.label}: create alias {name} = {a['cidr']}", _post(f"{base}/aliases", {"name": name, **data}))
        elif have[name].get("cidr") != a["cidr"] or (have[name].get("comment") or "") != (a.get("comment") or ""):
            plan.add(0, f"{scope.label}: alias {name} = {a['cidr']}", _put(f"{base}/aliases/{name}", data))
    for name in have.keys() - want.keys():
        plan.add(4, f"{scope.label}: remove alias {name}", _delete(f"{base}/aliases/{name}"))

    sets = await client.get(f"{base}/ipset") or []
    plan.seen[f"{base}/ipset"] = sets
    have_sets = {s["name"]: s for s in sets}
    for s in level.get("ipsets") or []:
        name = s["name"]
        entries = {e["cidr"]: e for e in s.get("entries") or []}
        if name not in have_sets:
            plan.add(0, f"{scope.label}: create IP set {name}",
                     _post(f"{base}/ipset", {"name": name, **({"comment": s["comment"]} if s.get("comment") else {})}))
            current: dict[str, Any] = {}
        else:
            listing = await client.get(f"{base}/ipset/{name}") or []
            plan.seen[f"{base}/ipset/{name}"] = listing
            current = {e["cidr"]: e for e in listing}
            if (have_sets[name].get("comment") or "") != (s.get("comment") or ""):
                plan.add(0, f"{scope.label}: IP set {name} comment",
                         _post(f"{base}/ipset", {"name": name, "rename": name, "comment": s.get("comment") or ""}))
        for cidr, e in entries.items():
            data = {"cidr": cidr, **{k: e[k] for k in ("comment", "nomatch") if e.get(k)}}
            if cidr not in current:
                plan.add(0, f"{scope.label}: add {'!' if e.get('nomatch') else ''}{cidr} to {name}",
                         _post(f"{base}/ipset/{name}", data))
            elif ((current[cidr].get("comment") or "") != (e.get("comment") or "")
                  or _flag(current[cidr].get("nomatch") or 0) != _flag(e.get("nomatch") or 0)):
                plan.add(0, f"{scope.label}: change {cidr} in {name}", _change_entry(base, name, cidr, data))
        for cidr in current.keys() - entries.keys():
            plan.add(3, f"{scope.label}: remove {cidr} from {name}", _delete(f"{base}/ipset/{name}/{cidr}"))
    for name in have_sets.keys() - {s["name"] for s in level.get("ipsets") or []}:
        plan.add(3, f"{scope.label}: remove IP set {name}", _delete(f"{base}/ipset/{name}", {"force": 1}))


def _change_entry(base: str, name: str, cidr: str, data: dict[str, Any]) -> Any:
    async def run(w: Writer) -> None:
        await w.put(f"{base}/ipset/{name}/{cidr}", {k: v for k, v in data.items() if k != "cidr"})
    return run


async def build_plan(client: ProxmoxClient, doc: dict[str, Any], force: bool = False) -> Plan:
    plan = Plan()
    if "datacenter" not in doc:
        check_refs(doc, {"alias": {a["name"] for a in await client.get("/cluster/firewall/aliases") or []},
                         "ipset": {s["name"] for s in await client.get("/cluster/firewall/ipset") or []},
                         "group": {g["group"] for g in await client.get("/cluster/firewall/groups") or []}})
    else:
        check_refs(doc, {"alias": set(), "ipset": set(), "group": set()})
    if "datacenter" in doc:
        level = doc["datacenter"] or {}
        await used_outside(client, doc, {
            "alias": sorted({a["name"] for a in await client.get("/cluster/firewall/aliases") or []}
                            - {a["name"] for a in level.get("aliases") or []}),
            "ipset": sorted({x["name"] for x in await client.get("/cluster/firewall/ipset") or []}
                            - {x["name"] for x in level.get("ipsets") or []}),
            "group": sorted({g["group"] for g in await client.get("/cluster/firewall/groups") or []}
                            - {g["name"] for g in level.get("groups") or []}),
        }, force)
        dc = cluster_scope()
        plan.scopes.append(dc)
        await _plan_objects(client, plan, dc, level)
        groups = await client.get("/cluster/firewall/groups") or []
        plan.seen["/cluster/firewall/groups"] = groups
        have = {g["group"] for g in groups}
        want = {g["name"]: g for g in level.get("groups") or []}
        for name, g in want.items():
            gs = group_scope(name)
            if name not in have:
                plan.add(0, f"datacenter: create security group {name}",
                         _post("/cluster/firewall/groups", {"group": name, **({"comment": g["comment"]} if g.get("comment") else {})}))
                current: list[dict[str, Any]] = []
            else:
                current = await read_rules(client, gs)
            _plan_rules(plan, gs, current, g.get("rules") or [])
        for name in have - want.keys():
            gs = group_scope(name)
            rules = await read_rules(client, gs)
            plan.seen[gs.rules_path] = rules
            for r in sorted(rules, key=lambda x: x["pos"], reverse=True):
                plan.add(2, f"security group {name}: remove rule {r['pos']}", _delete(f"{gs.rules_path}/{r['pos']}"))
            plan.add(2, f"datacenter: remove security group {name}", _delete(f"/cluster/firewall/groups/{name}"))
        _plan_rules(plan, dc, await read_rules(client, dc), level.get("rules") or [])
        _plan_options(plan, dc, await client.get(dc.options_path) or {}, level.get("options") or {})

    known_nodes = {n["node"] for n in await client.get_nodes() or []}
    for name, level in (doc.get("nodes") or {}).items():
        if name not in known_nodes:
            fail(f"nodes: '{name}' is not a node of this cluster")
        scope = node_scope(name)
        plan.scopes.append(scope)
        _plan_rules(plan, scope, await read_rules(client, scope), (level or {}).get("rules") or [])
        _plan_options(plan, scope, await client.get(scope.options_path) or {}, (level or {}).get("options") or {})

    guests = {g["vmid"]: g for g in await _guests(client)}
    for vmid, level in (doc.get("guests") or {}).items():
        g = guests.get(int(vmid))
        if g is None:
            fail(f"guests: {vmid} is not a VM or container of this cluster")
        kind = "vm" if g["type"] == "qemu" else "ct"
        level = level or {}
        if level.get("type") and level["type"] != kind:
            fail(f"guests: {vmid} is a {'VM' if kind == 'vm' else 'container'}, the file says {level['type']}")
        scope = guest_scope(kind, g["node"], int(vmid))
        plan.scopes.append(scope)
        await _plan_objects(client, plan, scope, level)
        _plan_rules(plan, scope, await read_rules(client, scope), level.get("rules") or [])
        _plan_options(plan, scope, await client.get(scope.options_path) or {}, level.get("options") or {})

    if doc.get("vnets"):
        vnets = set(await _vnets(client))
        for name, level in doc["vnets"].items():
            if name not in vnets:
                fail(f"vnets: '{name}' is not a vnet of this cluster")
            scope = vnet_scope(name)
            plan.scopes.append(scope)
            try:
                current = await read_rules(client, scope)
            except PVECliError as e:
                if is_not_implemented(e):
                    fail("vnets: this Proxmox VE has no vnet firewall (PVE 8.3 or later)")
                raise
            _plan_rules(plan, scope, current, (level or {}).get("rules") or [])
            _plan_options(plan, scope, await client.get(scope.options_path) or {}, (level or {}).get("options") or {})
    plan.steps.sort(key=lambda s: s.phase)
    return plan


def access_mutation(doc: dict[str, Any], api_node: str | None) -> Any:
    """How the file changes what decides the access to the API."""

    def mutate(inputs: dict[str, Any]) -> None:
        dc = doc.get("datacenter")
        if dc is not None:
            dc = dc or {}
            inputs["dc_opts"] = dict(dc.get("options") or {})
            inputs["dc_rules"] = [dict(r, pos=i) for i, r in enumerate(dc.get("rules") or [])]
            inputs["groups"] = {g["name"]: [dict(r, pos=i) for i, r in enumerate(g.get("rules") or [])]
                                for g in dc.get("groups") or []}
            inputs["aliases"] = {a["name"]: a["cidr"] for a in dc.get("aliases") or []}
            inputs["ipsets"] = {s["name"]: list(s.get("entries") or []) for s in dc.get("ipsets") or []}
        node = (doc.get("nodes") or {}).get(api_node) if api_node else None
        if api_node and api_node in (doc.get("nodes") or {}):
            node = node or {}
            inputs["node_opts"] = dict(node.get("options") or {})
            inputs["node_rules"] = [dict(r, pos=i) for i, r in enumerate(node.get("rules") or [])]

    return mutate


# ── the command ──────────────────────────────────────────────────────────


def add_import_command(app: typer.Typer) -> None:
    @app.command("import")
    @async_to_sync
    async def import_firewall(
        file: str = typer.Argument(..., help="A file written by cluster firewall export"),
        force: bool = typer.Option(False, "--force", "-f", help="Go on even if the API would be closed "
                                                                "to this machine, or objects removed are "
                                                                "still used by levels outside the file"),
        profile: str = typer.Option(None, "--profile", "-p", help="Profile to use"),
        yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation"),
    ) -> None:
        """Make the firewall of the cluster match a file: only the levels it names."""
        doc = load(file)
        profile_config = ConfigManager().get_profile(profile)
        try:
            async with ProxmoxClient(profile_config) as client:
                plan = await build_plan(client, doc, force)
                for scope in plan.scopes:
                    require_write(profile_config, profile, scope)
                if not plan.steps:
                    print_success("The firewall already matches the file")
                    return
                console.print(f"\n[bold]Make the firewall match {file}:[/bold]")
                for step in plan.steps:
                    console.print(f"  {step.text}", markup=False, highlight=False)
                from .firewall_edit import guarded_change
                from .firewall_guard import api_node

                try:
                    node = await api_node(client, profile_config.host)
                except PVECliError:
                    node = None
                watch, _ = await guarded_change(client, profile_config, force, access_mutation(doc, node))
                if not approved(yes):
                    return
                w = Writer(client, copy.deepcopy(plan.seen))
                for i, step in enumerate(plan.steps):
                    try:
                        await step.run(w)
                    except PVECliError as e:
                        print_error(f"Stopped at: {step.text}: {e}")
                        if i:
                            print_info(f"{i} of {len(plan.steps)} changes were made: run the import again "
                                       "to finish, it starts from what is there now")
                            # Half done, the access may already be another one
                            if watch:
                                from .firewall_guard import check_after

                                await check_after(profile_config, "the import stopped half way")
                        raise typer.Exit(1)
                print_success(f"{len(plan.steps)} change{'' if len(plan.steps) == 1 else 's'} made")
                if watch:
                    from .firewall_guard import check_after

                    await check_after(profile_config, f"the import of {file}")
        except PVECliError as e:
            fail(str(e))

