"""Changing the firewall: aliases, IP sets, security groups and rules.

Every change is shown as a plan and confirmed (-y for scripts). Proxmox
keeps a digest per list (rules, aliases, each IP set...): the first write
to each list is checked against the digest read for the plan, so a list
someone changed meanwhile is refused instead of edited by position.

What Proxmox does not protect, pvecli does:

- it lets an alias, an IP set or a security group in use be removed, and
  the rules that named it break (a broken ACCEPT is ignored, which can cut
  an access): pvecli looks for every user first and refuses;
- renaming an alias or an IP set does not follow its references: pvecli
  creates the new one, points every reference at it, then removes the old
  one, so no reference is ever broken in between.

Writes that reach the whole cluster (datacenter, security groups, nodes,
vnets) need `firewall_write: true` in the profile, like SDN writes. The
rules of a single guest do not.
"""

from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import dataclass
from typing import Any

import typer

from ..api.client import ProxmoxClient, is_not_implemented
from ..api.exceptions import PVECliError
from ..config import ConfigManager
from ..utils import (
    confirm,
    console,
    print_cancelled,
    print_error,
    print_info,
    print_success,
    print_warning,
)
from ..utils.helpers import async_to_sync
from ..utils.menu import multi_select_menu, select_menu
from .firewall import (
    Scope,
    _guests,
    _vnets,
    cluster_scope,
    describe_rule,
    group_scope,
    guest_scope,
    node_scope,
    read_rules,
    resolve,
    vnet_scope,
)

# Levels whose firewall reaches more than one guest
GATED = ("cluster", "group", "node", "vnet")
ACTIONS = ("ACCEPT", "DROP", "REJECT")
LOG_LEVELS = ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug", "nolog")
# Rule fields `rule edit --clear` can empty
CLEARABLE = ("macro", "proto", "source", "dest", "sport", "dport", "icmp-type", "iface", "log", "comment")


def require_write(profile_config: Any, profile: str | None, scope: Scope) -> None:
    """Refuse a write to a level that reaches the whole cluster, unless allowed."""
    if scope.kind not in GATED or getattr(profile_config, "firewall_write", False):
        return
    print_error(f"Firewall writes to {scope.label} are disabled for profile '{profile or 'default'}'.")
    print_info(
        "A change there reaches every guest or the access to the cluster itself. Add "
        "'firewall_write: true' to this profile in ~/.config/pvecli/config.yaml to allow it. "
        "The firewall of a single guest needs no such switch."
    )
    raise typer.Exit(1)


def section_of(method: str, path: str) -> str:
    """The list a write changes: Proxmox keeps one digest per list.

    The options, the rules, the aliases and the IP sets of a level, each IP
    set, the security groups and each group have their own digest (PVE 9.1).
    """
    parts = path.rstrip("/").split("/")
    i = parts.index("firewall")
    head, rest = parts[: i + 1], parts[i + 1:]
    if rest[0] in ("ipset", "groups") and len(rest) >= 2:
        # A set or a group itself: created or removed in the list of them
        if len(rest) == 2 and not (method == "POST"):
            return "/".join(head + rest[:1])
        return "/".join(head + rest[:2])
    return "/".join(head + rest[:1])


class Writer:
    """Sends the writes of a plan, guarded by the digests seen for the plan.

    A digest describes one list as it was read for the plan. The first write
    to each list carries it, so a list someone changed since is refused;
    when that write takes no digest, the list is read again and compared
    just before. Later writes to the same list go without: it has changed
    by our own hand.
    """

    def __init__(self, client: ProxmoxClient, seen: dict[str, Any] | None = None) -> None:
        self.client = client
        self.digests: dict[str, str] = {}
        for section, listing in (seen or {}).items():
            digest = _digest_of(listing)
            if digest:
                self.digests[section] = digest
        self.written: set[str] = set()

    async def snapshot(self, sections: list[str]) -> None:
        """Read the digests of lists the command did not read itself."""
        for section in dict.fromkeys(sections):
            if section not in self.digests:
                digest = _digest_of(await self.client.get(section))
                if digest:
                    self.digests[section] = digest

    async def _guard(self, method: str, path: str, data: dict[str, Any]) -> dict[str, Any]:
        section = section_of(method, path)
        if section in self.written:
            return data
        self.written.add(section)
        planned = self.digests.get(section)
        if not planned:
            return data
        if takes_digest(method, path):
            return {**data, "digest": planned}
        if _digest_of(await self.client.get(section)) != planned:
            raise PVECliError(f"{section} changed since the plan was made: run the command again")
        return data

    async def post(self, path: str, data: dict[str, Any]) -> Any:
        return await self.client.post(path, data=await self._guard("POST", path, data))

    async def put(self, path: str, data: dict[str, Any]) -> Any:
        return await self.client.put(path, data=await self._guard("PUT", path, data))

    async def delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.client.delete(path, params=await self._guard("DELETE", path, params or {}) or None)


def takes_digest(method: str, path: str) -> bool:
    """Whether Proxmox accepts a digest on this write (PVE 9.1 schema).

    Creating an alias, adding an IP set entry, removing a whole IP set or a
    security group take none; every other firewall write does.
    """
    parts = path.rstrip("/").split("/")
    if method == "POST" and parts[-1] == "aliases":
        return False
    if method in ("POST", "DELETE") and len(parts) >= 2 and parts[-2] == "ipset":
        return False
    return not (method == "DELETE" and len(parts) >= 2 and parts[-2] == "groups")


def show_plan(title: str, lines: list[str]) -> None:
    console.print(f"\n[bold]{title}[/bold]")
    for line in lines:
        console.print(f"  {line}", markup=False, highlight=False)


def approved(yes: bool, question: str = "Apply?") -> bool:
    if yes or confirm(question):
        return True
    print_cancelled()
    return False


def _digest_of(items: Any) -> str | None:
    if isinstance(items, dict):
        return items.get("digest")
    for i in items or []:
        if i.get("digest"):
            return str(i["digest"])
    return None


# ── references ───────────────────────────────────────────────────────────


@dataclass
class Ref:
    """One place that names an object: a rule field or an IP set entry."""

    scope: Scope  # the level holding the rule or the IP set
    kind: str  # "rule" or "entry"
    pos: int | None  # rule position
    field: str  # source, dest, action, or the IP set name for an entry
    value: str
    entry: dict[str, Any] | None = None
    # The forms that name the object here: on a guest with its own object
    # of that name, only the dc/ form names the datacenter one
    forms: frozenset[str] = frozenset()

    def where(self) -> str:
        if self.kind == "rule":
            return f"{self.scope.label}, rule {self.pos} ({self.field})"
        return f"{self.scope.label}, IP set {self.field}"


def ref_forms(obj: str, name: str, owner: Scope, at: Scope, shadowed: bool) -> set[str]:
    """How a rule at `at` writes a reference to the object `name` of `owner`.

    obj is "alias" or "ipset". A guest reads a bare name as its own object
    first, and as the datacenter one only when it has none of that name.
    """
    plus = "+" if obj == "ipset" else ""
    if owner.kind == "cluster":
        forms = {f"{plus}dc/{name}"}
        if not (at.is_guest and shadowed):
            forms.add(f"{plus}{name}")
        return forms
    # An object of a guest is seen from that guest only
    if at.kind != owner.kind or at.vmid != owner.vmid:
        return set()
    return {f"{plus}guest/{name}", f"{plus}{name}"}


def _tokens(value: str) -> list[str]:
    return [t.strip() for t in str(value).split(",")]


def renamed(value: str, old: set[str], new_name: str) -> str:
    """The value with each reference in `old` pointing at new_name instead.

    Each reference keeps its form: +dc/web becomes +dc/front, web becomes
    front. On a guest a bare name and a dc/ name are not the same object.
    """

    def one(token: str) -> str:
        if token not in old:
            return token
        plus = "+" if token.startswith("+") else ""
        body = token[len(plus):]
        prefix = body.split("/", 1)[0] + "/" if "/" in body else ""
        return f"{plus}{prefix}{new_name}"

    return ",".join(one(t) for t in _tokens(value))


async def _all_levels(client: ProxmoxClient, owner: Scope) -> list[Scope]:
    """The levels whose rules can name an object of `owner`."""
    if owner.is_guest:
        return [owner]
    nodes = sorted(n["node"] for n in await client.get_nodes() or [] if n.get("status") == "online")
    groups = [g["group"] for g in await client.get("/cluster/firewall/groups") or []]
    guests = await _guests(client)
    return (
        [cluster_scope()] + [group_scope(g) for g in sorted(groups)] + [node_scope(n) for n in nodes]
        + [guest_scope("vm" if g["type"] == "qemu" else "ct", g["node"], g["vmid"]) for g in guests]
        + [vnet_scope(v) for v in await _vnets(client)]
    )


async def find_refs(client: ProxmoxClient, obj: str, name: str, owner: Scope) -> list[Ref]:
    """Every rule and IP set entry that names this alias, IP set or group."""
    levels = await _all_levels(client, owner)

    async def rules_of(scope: Scope) -> list[dict[str, Any]]:
        try:
            return await read_rules(client, scope)
        except PVECliError as e:
            if is_not_implemented(e) or getattr(e, "status_code", None) in (404, 500):
                return []
            raise

    all_rules = await asyncio.gather(*(rules_of(s) for s in levels))
    refs: list[Ref] = []
    for scope, rules in zip(levels, all_rules, strict=True):
        shadowed = False
        if scope.is_guest and owner.kind == "cluster" and obj != "group":
            own = await client.get(f"{scope.base}/{'aliases' if obj == 'alias' else 'ipset'}") or []
            shadowed = any(o.get("name") == name for o in own)
        forms = set() if obj == "group" else ref_forms(obj, name, owner, scope, shadowed)
        for r in rules:
            if obj == "group":
                if r.get("type") == "group" and r.get("action") == name:
                    refs.append(Ref(scope, "rule", r.get("pos"), "action", name))
                continue
            for fld in ("source", "dest"):
                if r.get(fld) and forms & set(_tokens(r[fld])):
                    refs.append(Ref(scope, "rule", r.get("pos"), fld, r[fld], forms=frozenset(forms)))
    # An alias can also be an entry of an IP set
    if obj == "alias":
        holders = [s for s in levels if s.kind == "cluster" or s.is_guest]
        for scope in holders:
            shadowed = False
            if scope.is_guest and owner.kind == "cluster":
                own = await client.get(f"{scope.base}/aliases") or []
                shadowed = any(o.get("name") == name for o in own)
            forms = ref_forms("alias", name, owner, scope, shadowed)
            if not forms:
                continue
            for s in await client.get(f"{scope.base}/ipset") or []:
                for e in await client.get(f"{scope.base}/ipset/{s['name']}") or []:
                    if e.get("cidr") in forms:
                        refs.append(Ref(scope, "entry", None, s["name"], e["cidr"], e, frozenset(forms)))
    return refs


def refuse_if_used(what: str, refs: list[Ref], force: bool) -> None:
    if not refs:
        return
    console.print(f"\n[bold red]{what} is used by:[/bold red]")
    for r in refs:
        console.print(f"  {r.where()}", markup=False, highlight=False)
    if force:
        console.print("  [yellow]--force: these references will break, and Proxmox will ignore "
                      "the rules that hold them[/yellow]")
        return
    print_error(f"{what} is in use: change or remove those references first, or pass --force")
    raise typer.Exit(1)


async def rewrite_refs(w: Writer, refs: list[Ref], old: set[str], new_name: str) -> None:
    """Point every reference at the new name: rules in place, entries re-added."""
    for r in refs:
        value = renamed(r.value, set(r.forms) or old, new_name)
        if r.kind == "rule":
            await w.put(f"{r.scope.rules_path}/{r.pos}", {r.field: value})
        else:
            assert r.entry is not None
            entry = {k: v for k, v in r.entry.items() if k in ("comment", "nomatch")}
            # The new entry first: the set never lacks the address
            await w.post(f"{r.scope.base}/ipset/{r.field}", {"cidr": value, **entry})
            await w.delete(f"{r.scope.base}/ipset/{r.field}/{r.value}")


# ── checks of user input ─────────────────────────────────────────────────


def check_name(name: str, what: str) -> str:
    """Proxmox names: a letter first, then letters, digits, - and _ (max 64)."""
    import re

    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9\-_]{1,63}", name or ""):
        print_error(f"Invalid {what} name '{name}': a letter, then letters, digits, - or _ (2 to 64)")
        raise typer.Exit(1)
    return name


def check_cidr(value: str, allow_alias: bool = False) -> str:
    """An address, a network or a range; an alias name where Proxmox takes one."""
    try:
        ipaddress.ip_network(value, strict=False)
        return value
    except ValueError:
        pass
    if "-" in value:
        lo, _, hi = value.partition("-")
        try:
            ipaddress.ip_address(lo)
            ipaddress.ip_address(hi)
            return value
        except ValueError:
            pass
    if allow_alias:
        return value
    print_error(f"Not an address or a network: {value}")
    raise typer.Exit(1)


# ── the commands ─────────────────────────────────────────────────────────


async def level_session(profile: str | None, kind: str, target: str | None, body: Any) -> None:
    """Open the client, find the level, check the profile may write there, run body."""
    profile_config = ConfigManager().get_profile(profile)
    async with ProxmoxClient(profile_config) as client:
        scope = await resolve(client, kind, target)
        if scope is None:
            print_cancelled()
            return
        require_write(profile_config, profile, scope)
        await body(client, scope)


def _fail(e: PVECliError) -> None:
    print_error(str(e))
    raise typer.Exit(1)


YES = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation")
PROFILE = typer.Option(None, "--profile", "-p", help="Profile to use")
COMMENT = typer.Option(None, "--comment", "-c", help="Comment")


async def guard_dc(client: ProxmoxClient, profile: str | None, scope: Scope, force: bool, mutate: Any) -> bool:
    """The access guard for a change of a datacenter object.

    Aliases, IP sets and groups of the datacenter decide who reaches the API
    through the management set and the rules of the datacenter and nodes.
    Returns whether to check the access once the change is applied.
    """
    if scope.kind not in ("cluster", "group"):
        return False
    watch, _ = await guarded_change(client, ConfigManager().get_profile(profile), force, mutate)
    return watch


async def check_dc(profile: str | None, what: str) -> None:
    from .firewall_guard import check_after

    await check_after(ConfigManager().get_profile(profile), what)


def rename_in(inputs: dict[str, Any], obj: str, old: str, new: str) -> None:
    """In the inputs of the guard: a datacenter object renamed, its references followed."""
    forms = ref_forms_all(obj, old, cluster_scope())

    def follow(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**r, **{f: renamed(r[f], forms, new) for f in ("source", "dest") if r.get(f)}} for r in rules]

    inputs["dc_rules"] = follow(inputs["dc_rules"])
    inputs["node_rules"] = follow(inputs["node_rules"])
    inputs["groups"] = {g: follow(rs) for g, rs in inputs["groups"].items()}
    if obj == "alias":
        inputs["aliases"][new] = inputs["aliases"].pop(old)
        inputs["ipsets"] = {n: [{**e, "cidr": renamed(str(e.get("cidr", "")), forms, new)} for e in es]
                            for n, es in inputs["ipsets"].items()}
    else:
        inputs["ipsets"][new] = inputs["ipsets"].pop(old, [])


def add_object_commands(alias_app: typer.Typer, ipset_app: typer.Typer, kind: str,
                        target_arg: Any, drop: Any) -> None:
    """alias add/edit/remove and ipset add/edit/remove for one kind of level."""

    async def session(profile: str | None, target: str | None, body: Any) -> None:
        await level_session(profile, kind, target, body)

    # ── aliases ──
    @alias_app.command("add")
    @drop
    @async_to_sync
    async def alias_add(
        target: str = target_arg,
        name: str = typer.Argument(..., help="Alias name"),
        cidr: str = typer.Argument(..., help="Address or network"),
        comment: str = COMMENT,
        profile: str = PROFILE,
        yes: bool = YES,
    ) -> None:
        """Create an alias: a name for an address or a network."""
        check_name(name, "alias")
        check_cidr(cidr)

        async def body(client: ProxmoxClient, scope: Scope) -> None:
            existing = await client.get(f"{scope.base}/aliases") or []
            if any(a.get("name") == name for a in existing):
                print_error(f"Alias '{name}' already exists on {scope.label}")
                raise typer.Exit(1)
            show_plan(f"Create on {scope.label}:", [f"alias {name} = {cidr}" + (f"  # {comment}" if comment else "")])
            if not approved(yes):
                return
            data = {"name": name, "cidr": cidr, **({"comment": comment} if comment else {})}
            await Writer(client, {f"{scope.base}/aliases": existing}).post(f"{scope.base}/aliases", data)
            print_success(f"Alias '{name}' created on {scope.label}")

        try:
            await session(profile, target, body)
        except PVECliError as e:
            _fail(e)

    @alias_app.command("edit")
    @drop
    @async_to_sync
    async def alias_edit(
        target: str = target_arg,
        name: str | None = typer.Argument(None, help="Alias name"),
        cidr: str = typer.Option(None, "--cidr", help="New address or network"),
        comment: str = COMMENT,
        rename: str = typer.Option(None, "--rename", help="New name: every rule that names the alias follows"),
        profile: str = PROFILE,
        yes: bool = YES,
    ) -> None:
        """Change the address, the comment or the name of an alias."""

        async def body(client: ProxmoxClient, scope: Scope) -> None:
            nonlocal name
            aliases = await client.get(f"{scope.base}/aliases") or []
            name = name or _pick([a["name"] for a in aliases], "alias")
            if name is None:
                return
            current = next((a for a in aliases if a.get("name") == name), None)
            if current is None:
                print_error(f"Alias '{name}' not found on {scope.label}")
                raise typer.Exit(1)
            if cidr is None and comment is None and rename is None:
                print_error("Nothing to change: pass --cidr, --comment or --rename")
                raise typer.Exit(1)
            if cidr:
                check_cidr(cidr)
            new_cidr = cidr or current["cidr"]
            new_comment = current.get("comment", "") if comment is None else comment
            lines = []
            if cidr and cidr != current["cidr"]:
                lines.append(f"alias {name}: {current['cidr']} -> {cidr}")
            if comment is not None:
                lines.append(f"alias {name}: comment '{new_comment}'")
            refs: list[Ref] = []
            if rename and rename != name:
                check_name(rename, "alias")
                if any(a.get("name") == rename for a in aliases):
                    print_error(f"Alias '{rename}' already exists on {scope.label}")
                    raise typer.Exit(1)
                refs = await find_refs(client, "alias", name, scope)
                lines.append(f"create alias {rename} = {new_cidr}")
                lines += [f"point {r.where()} at {rename}" for r in refs]
                lines.append(f"remove alias {name}")
            w = Writer(client, {f"{scope.base}/aliases": aliases})
            await w.snapshot(ref_sections(refs))
            show_plan(f"Change on {scope.label}:", lines)

            def mutate(inputs: dict[str, Any]) -> None:
                inputs["aliases"][name] = new_cidr
                if rename and rename != name:
                    rename_in(inputs, "alias", name, rename)

            watch = await guard_dc(client, profile, scope, False, mutate)
            if not approved(yes):
                return
            data = {"cidr": new_cidr, **({"comment": new_comment} if new_comment else {})}
            if rename and rename != name:
                await w.post(f"{scope.base}/aliases", {"name": rename, **data})
                await rewrite_refs(w, refs, ref_forms_all("alias", name, scope), rename)
                await w.delete(f"{scope.base}/aliases/{name}")
            else:
                await w.put(f"{scope.base}/aliases/{name}", data)
            print_success(f"Alias '{rename or name}' updated on {scope.label}")
            if watch:
                await check_dc(profile, f"the change to the alias {name}")

        try:
            await session(profile, target, body)
        except PVECliError as e:
            _fail(e)

    @alias_app.command("remove")
    @drop
    @async_to_sync
    async def alias_remove(
        target: str = target_arg,
        name: str | None = typer.Argument(None, help="Alias name"),
        force: bool = typer.Option(False, "--force", "-f", help="Remove it even if rules name it"),
        profile: str = PROFILE,
        yes: bool = YES,
    ) -> None:
        """Remove an alias, unless a rule or an IP set still names it."""

        async def body(client: ProxmoxClient, scope: Scope) -> None:
            nonlocal name
            aliases = await client.get(f"{scope.base}/aliases") or []
            name = name or _pick([a["name"] for a in aliases], "alias")
            if name is None:
                return
            if not any(a.get("name") == name for a in aliases):
                print_error(f"Alias '{name}' not found on {scope.label}")
                raise typer.Exit(1)
            refuse_if_used(f"Alias '{name}'", await find_refs(client, "alias", name, scope), force)
            show_plan(f"Remove from {scope.label}:", [f"alias {name}"])
            watch = await guard_dc(client, profile, scope, force,
                                   lambda inputs: inputs["aliases"].pop(name, None))
            if not approved(yes):
                return
            await Writer(client, {f"{scope.base}/aliases": aliases}).delete(f"{scope.base}/aliases/{name}")
            print_success(f"Alias '{name}' removed from {scope.label}")
            if watch:
                await check_dc(profile, f"the removal of the alias {name}")

        try:
            await session(profile, target, body)
        except PVECliError as e:
            _fail(e)

    # ── IP sets ──
    @ipset_app.command("add")
    @drop
    @async_to_sync
    async def ipset_add(
        target: str = target_arg,
        name: str = typer.Argument(..., help="IP set name (created if missing)"),
        cidrs: list[str] = typer.Argument(None, help="Addresses, networks or alias names to add"),
        nomatch: bool = typer.Option(False, "--nomatch", help="Add them as exceptions (never match)"),
        comment: str = typer.Option(None, "--comment", "-c", help="Comment of the entries, or of a new set"),
        profile: str = PROFILE,
        yes: bool = YES,
    ) -> None:
        """Create an IP set and add entries to it."""
        check_name(name, "IP set")
        cidrs = [check_cidr(c, allow_alias=True) for c in cidrs or []]

        async def body(client: ProxmoxClient, scope: Scope) -> None:
            sets = await client.get(f"{scope.base}/ipset") or []
            exists = any(s.get("name") == name for s in sets)
            entries = (await client.get(f"{scope.base}/ipset/{name}") or []) if exists else []
            present = {e.get("cidr") for e in entries}
            todo = [c for c in cidrs if c not in present]
            lines = [] if exists else [f"create IP set {name}" + (f"  # {comment}" if comment and not cidrs else "")]
            lines += [f"add {'!' if nomatch else ''}{c} to {name}" for c in todo]
            lines += [f"(already in {name}: {c})" for c in cidrs if c in present]
            if exists and not todo:
                print_info(f"Nothing to add: IP set '{name}' already holds every entry")
                return
            show_plan(f"Change on {scope.label}:", lines)
            if scope.kind == "cluster" and name == "management":
                console.print("  [yellow]The 'management' IP set opens the web interface and SSH of "
                              "every node when the firewall is on: once it exists, only its members "
                              "are management hosts.[/yellow]")
            added = [{"cidr": c, **({"nomatch": 1} if nomatch else {})} for c in todo]

            def mutate(inputs: dict[str, Any]) -> None:
                # Any set can be named by a rule of the node or of the datacenter
                inputs["ipsets"][name] = list(entries) + added

            watch = await guard_dc(client, profile, scope, False, mutate)
            if not approved(yes):
                return
            w = Writer(client, {f"{scope.base}/ipset": sets, f"{scope.base}/ipset/{name}": entries})
            if not exists:
                await w.post(f"{scope.base}/ipset",
                             {"name": name, **({"comment": comment} if comment and not cidrs else {})})
            for c in todo:
                data: dict[str, Any] = {"cidr": c}
                if nomatch:
                    data["nomatch"] = 1
                if comment and cidrs:
                    data["comment"] = comment
                await w.post(f"{scope.base}/ipset/{name}", data)
            print_success(f"IP set '{name}' on {scope.label}: {len(todo)} entr{'y' if len(todo) == 1 else 'ies'} added")
            if watch:
                from .firewall_guard import check_after

                await check_after(ConfigManager().get_profile(profile), f"the change to the IP set {name}")

        try:
            await session(profile, target, body)
        except PVECliError as e:
            _fail(e)

    @ipset_app.command("edit")
    @drop
    @async_to_sync
    async def ipset_edit(
        target: str = target_arg,
        name: str | None = typer.Argument(None, help="IP set name"),
        comment: str = COMMENT,
        rename: str = typer.Option(None, "--rename", help="New name: every rule that names the set follows"),
        profile: str = PROFILE,
        yes: bool = YES,
    ) -> None:
        """Change the comment or the name of an IP set."""

        async def body(client: ProxmoxClient, scope: Scope) -> None:
            nonlocal name
            sets = await client.get(f"{scope.base}/ipset") or []
            name = name or _pick([s["name"] for s in sets], "IP set")
            if name is None:
                return
            current = next((s for s in sets if s.get("name") == name), None)
            if current is None:
                print_error(f"IP set '{name}' not found on {scope.label}")
                raise typer.Exit(1)
            if comment is None and rename is None:
                print_error("Nothing to change: pass --comment or --rename")
                raise typer.Exit(1)
            new_comment = current.get("comment", "") if comment is None else comment
            if not rename or rename == name:
                show_plan(f"Change on {scope.label}:", [f"IP set {name}: comment '{new_comment}'"])
                if not approved(yes):
                    return
                await Writer(client, {f"{scope.base}/ipset": sets}).post(f"{scope.base}/ipset", {"name": name, "rename": name, "comment": new_comment})
                print_success(f"IP set '{name}' updated on {scope.label}")
                return
            check_name(rename, "IP set")
            if any(s.get("name") == rename for s in sets):
                print_error(f"IP set '{rename}' already exists on {scope.label}")
                raise typer.Exit(1)
            entries = await client.get(f"{scope.base}/ipset/{name}") or []
            refs = await find_refs(client, "ipset", name, scope)
            lines = [f"create IP set {rename} with the {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} of {name}"]
            lines += [f"point {r.where()} at +{rename}" for r in refs]
            lines.append(f"remove IP set {name}")
            w = Writer(client, {f"{scope.base}/ipset": sets})
            await w.snapshot(ref_sections(refs))
            show_plan(f"Change on {scope.label}:", lines)
            if scope.kind == "cluster" and name == "management":
                console.print("  [yellow]Renamed, 'management' no longer exists: the management hosts become "
                              "the cluster network.[/yellow]")
            watch = await guard_dc(client, profile, scope, False,
                                   lambda inputs: rename_in(inputs, "ipset", name, rename))
            if not approved(yes):
                return
            await w.post(f"{scope.base}/ipset", {"name": rename, **({"comment": new_comment} if new_comment else {})})
            for e in entries:
                await w.post(f"{scope.base}/ipset/{rename}",
                             {k: v for k, v in e.items() if k in ("cidr", "comment", "nomatch")})
            await rewrite_refs(w, refs, ref_forms_all("ipset", name, scope), rename)
            await w.delete(f"{scope.base}/ipset/{name}", {"force": 1})
            print_success(f"IP set '{name}' renamed to '{rename}' on {scope.label}")
            if watch:
                await check_dc(profile, f"the renaming of the IP set {name}")

        try:
            await session(profile, target, body)
        except PVECliError as e:
            _fail(e)

    @ipset_app.command("remove")
    @drop
    @async_to_sync
    async def ipset_remove(
        target: str = target_arg,
        name: str | None = typer.Argument(None, help="IP set name"),
        cidrs: list[str] = typer.Argument(None, help="Entries to remove; none removes the whole set"),
        force: bool = typer.Option(False, "--force", "-f", help="Remove the set even if rules name it"),
        profile: str = PROFILE,
        yes: bool = YES,
    ) -> None:
        """Remove entries from an IP set, or the whole set."""

        async def body(client: ProxmoxClient, scope: Scope) -> None:
            nonlocal name
            sets = await client.get(f"{scope.base}/ipset") or []
            name = name or _pick([s["name"] for s in sets], "IP set")
            if name is None:
                return
            if not any(s.get("name") == name for s in sets):
                print_error(f"IP set '{name}' not found on {scope.label}")
                raise typer.Exit(1)
            entries = await client.get(f"{scope.base}/ipset/{name}") or []
            w = Writer(client, {f"{scope.base}/ipset": sets, f"{scope.base}/ipset/{name}": entries})
            if cidrs:
                present = {e.get("cidr") for e in entries}
                missing = [c for c in cidrs if c not in present]
                if missing:
                    print_error(f"Not in IP set '{name}': {', '.join(missing)}")
                    raise typer.Exit(1)
                show_plan(f"Change on {scope.label}:", [f"remove {c} from {name}" for c in cidrs])
                if scope.kind == "cluster" and name == "management":
                    console.print("  [yellow]Removing an address from 'management' closes the web "
                                  "interface and SSH of every node to it when the firewall is on.[/yellow]")

                def mutate(inputs: dict[str, Any]) -> None:
                    inputs["ipsets"][name] = [e for e in entries if e.get("cidr") not in cidrs]

                watch = await guard_dc(client, profile, scope, force, mutate)
                if not approved(yes):
                    return
                for c in cidrs:
                    await w.delete(f"{scope.base}/ipset/{name}/{c}")
                print_success(f"{len(cidrs)} entr{'y' if len(cidrs) == 1 else 'ies'} removed from '{name}'")
                if watch:
                    from .firewall_guard import check_after

                    await check_after(ConfigManager().get_profile(profile), f"the change to the IP set {name}")
                return
            refuse_if_used(f"IP set '{name}'", await find_refs(client, "ipset", name, scope), force)
            show_plan(f"Remove from {scope.label}:",
                      [f"IP set {name} and its {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}"])
            if scope.kind == "cluster" and name == "management":
                console.print("  [yellow]Without a 'management' IP set, the management hosts are the "
                              "cluster network.[/yellow]")
            watch = await guard_dc(client, profile, scope, force,
                                   lambda inputs: inputs["ipsets"].pop(name, None))
            if not approved(yes):
                return
            await w.delete(f"{scope.base}/ipset/{name}", {"force": 1})
            print_success(f"IP set '{name}' removed from {scope.label}")
            if watch:
                from .firewall_guard import check_after

                await check_after(ConfigManager().get_profile(profile), f"the removal of the IP set {name}")

        try:
            await session(profile, target, body)
        except PVECliError as e:
            _fail(e)


def ref_forms_all(obj: str, name: str, owner: Scope) -> set[str]:
    """Every way any level writes a reference to this object."""
    plus = "+" if obj == "ipset" else ""
    if owner.kind == "cluster":
        return {f"{plus}dc/{name}", f"{plus}{name}"}
    return {f"{plus}guest/{name}", f"{plus}{name}"}


def ref_sections(refs: list[Ref]) -> list[str]:
    """The lists a rewrite of these references writes to."""
    return [r.scope.rules_path if r.kind == "rule" else f"{r.scope.base}/ipset/{r.field}" for r in refs]


def _pick(names: list[str], what: str) -> str | None:
    if not names:
        print_info(f"No {what}")
        return None
    names = sorted(names)
    idx = select_menu(names, f"  Select an {what}:" if what[0] in "aeiouAEIOU" else f"  Select a {what}:")
    if idx is None:
        print_cancelled()
        return None
    return names[idx]


def add_group_commands(group_app: typer.Typer) -> None:
    """group add/rename/remove: datacenter only."""

    async def session(profile: str | None, body: Any) -> None:
        profile_config = ConfigManager().get_profile(profile)
        async with ProxmoxClient(profile_config) as client:
            require_write(profile_config, profile, cluster_scope())
            await body(client)

    @group_app.command("add")
    @async_to_sync
    async def group_add(
        name: str = typer.Argument(..., help="Security group name"),
        comment: str = COMMENT,
        profile: str = PROFILE,
        yes: bool = YES,
    ) -> None:
        """Create an empty security group."""
        check_name(name, "security group")

        async def body(client: ProxmoxClient) -> None:
            groups = await client.get("/cluster/firewall/groups") or []
            if any(g.get("group") == name for g in groups):
                print_error(f"Security group '{name}' already exists")
                raise typer.Exit(1)
            show_plan("Create on datacenter:", [f"security group {name}" + (f"  # {comment}" if comment else "")])
            if not approved(yes):
                return
            await Writer(client, {"/cluster/firewall/groups": groups}).post("/cluster/firewall/groups", {"group": name, **({"comment": comment} if comment else {})})
            print_success(f"Security group '{name}' created")

        try:
            await session(profile, body)
        except PVECliError as e:
            _fail(e)

    @group_app.command("rename")
    @async_to_sync
    async def group_rename(
        name: str = typer.Argument(..., help="Security group name"),
        new: str = typer.Argument(..., help="New name"),
        profile: str = PROFILE,
        yes: bool = YES,
    ) -> None:
        """Rename a security group (the rules that insert it follow)."""
        check_name(new, "security group")

        async def body(client: ProxmoxClient) -> None:
            groups = await client.get("/cluster/firewall/groups") or []
            current = next((g for g in groups if g.get("group") == name), None)
            if current is None:
                print_error(f"Security group '{name}' not found")
                raise typer.Exit(1)
            if any(g.get("group") == new for g in groups):
                print_error(f"Security group '{new}' already exists")
                raise typer.Exit(1)
            refs = await find_refs(client, "group", name, cluster_scope())
            show_plan("Change on datacenter:", [f"security group {name} -> {new}"]
                      + [f"(follows: {r.where()})" for r in refs])
            if not approved(yes):
                return
            # Proxmox renames the group and the rules that insert it at once
            await Writer(client, {"/cluster/firewall/groups": groups}).post("/cluster/firewall/groups",
                {"group": new, "rename": name, **({"comment": current["comment"]} if current.get("comment") else {})})
            print_success(f"Security group '{name}' renamed to '{new}'")

        try:
            await session(profile, body)
        except PVECliError as e:
            _fail(e)

    @group_app.command("remove")
    @async_to_sync
    async def group_remove(
        name: str | None = typer.Argument(None, help="Security group name"),
        force: bool = typer.Option(False, "--force", "-f", help="Remove it even if rules insert it"),
        profile: str = PROFILE,
        yes: bool = YES,
    ) -> None:
        """Remove a security group and its rules, unless a level still inserts it."""

        async def body(client: ProxmoxClient) -> None:
            nonlocal name
            groups = await client.get("/cluster/firewall/groups") or []
            name = name or _pick([g["group"] for g in groups], "security group")
            if name is None:
                return
            if not any(g.get("group") == name for g in groups):
                print_error(f"Security group '{name}' not found")
                raise typer.Exit(1)
            refuse_if_used(f"Security group '{name}'", await find_refs(client, "group", name, cluster_scope()), force)
            gscope = group_scope(name)
            rules = await read_rules(client, gscope)
            show_plan("Remove from datacenter:", [f"security group {name}"]
                      + [f"with its rule {r.get('pos')}: {describe_rule(r)}" for r in rules])
            # With --force, rules that insert it lose what it let in
            watch = await guard_dc(client, profile, cluster_scope(), force,
                                   lambda inputs: inputs["groups"].pop(name, None))
            if not approved(yes):
                return
            w = Writer(client, {"/cluster/firewall/groups": groups, gscope.rules_path: rules})
            # Proxmox removes an empty group only: its rules go first, last one first
            for r in sorted(rules, key=lambda x: x.get("pos", 0), reverse=True):
                await w.delete(f"{gscope.rules_path}/{r['pos']}")
            await w.delete(f"/cluster/firewall/groups/{name}")
            print_success(f"Security group '{name}' removed")
            if watch:
                await check_dc(profile, f"the removal of the security group {name}")

        try:
            await session(profile, body)
        except PVECliError as e:
            _fail(e)


# ── rules ────────────────────────────────────────────────────────────────


def _rule_options() -> dict[str, Any]:
    return {
        "action": typer.Option(None, "--action", "-ac",
                               help="ACCEPT, DROP, REJECT, or a security group to insert"),
        "direction": typer.Option(None, "--direction", "-dn", help="in, out or forward"),
        "macro": typer.Option(None, "--macro", "-ma", help="A macro (cluster firewall macros): SSH, HTTPS..."),
        "proto": typer.Option(None, "--proto", "-pt", help="Protocol: tcp, udp, icmp... or a number"),
        "source": typer.Option(None, "--source", "-src", help="Source: address, network, alias or +ipset"),
        "dest": typer.Option(None, "--dest", "-dst", help="Destination: address, network, alias or +ipset"),
        "sport": typer.Option(None, "--sport", "-spt", help="Source port(s): 80, 80:90, 22,80"),
        "dport": typer.Option(None, "--dport", "-dpt", help="Destination port(s): 443, 8000:8100, 22,443"),
        "icmp_type": typer.Option(None, "--icmp-type", help="ICMP type, with --proto icmp"),
        "iface": typer.Option(None, "--iface", help="Interface: net0 on a guest, vmbr0 on a node"),
        "log": typer.Option(None, "--log", "-lg", help="Log level: " + ", ".join(LOG_LEVELS)),
        "comment": COMMENT,
    }


_OPT = _rule_options()


def rule_fields(direction: str | None, action: str | None, macro: str | None, proto: str | None,
                source: str | None, dest: str | None, sport: str | None, dport: str | None,
                icmp_type: str | None, iface: str | None, log: str | None, comment: str | None,
                scope: Scope, groups: list[str]) -> dict[str, Any]:
    """The API fields of the options given, checked. Only what was passed."""
    data: dict[str, Any] = {}
    if action is not None:
        if action.upper() in ACTIONS:
            data["action"] = action.upper()
        elif action in groups:
            if scope.kind == "group":
                print_error("A security group cannot insert another one")
                raise typer.Exit(1)
            data["action"] = action
            data["type"] = "group"
        else:
            print_error(f"Unknown action '{action}': ACCEPT, DROP, REJECT or a security group "
                        f"({', '.join(groups) or 'none defined'})")
            raise typer.Exit(1)
    if direction is not None:
        if data.get("type") == "group":
            print_error("A security group carries its own directions: --direction does not apply")
            raise typer.Exit(1)
        allowed = scope.directions if scope.kind != "group" else ("in", "out", "forward")
        if direction not in allowed:
            print_error(f"Direction '{direction}' is not possible on {scope.label}: {', '.join(allowed)}")
            raise typer.Exit(1)
        data["type"] = direction
    if log is not None and log not in LOG_LEVELS:
        print_error(f"Unknown log level '{log}': {', '.join(LOG_LEVELS)}")
        raise typer.Exit(1)
    for key, value in (("macro", macro), ("proto", proto), ("source", source), ("dest", dest),
                       ("sport", sport), ("dport", dport), ("icmp-type", icmp_type), ("iface", iface),
                       ("log", log), ("comment", comment)):
        if value is not None:
            data[key] = value
    if data.get("type") == "group" and any(k in data for k in ("macro", "proto", "sport", "dport", "icmp-type")):
        print_error("A rule that inserts a security group takes no macro, protocol nor port")
        raise typer.Exit(1)
    return data


async def _group_names(client: ProxmoxClient) -> list[str]:
    return sorted(g.get("group", "") for g in await client.get("/cluster/firewall/groups") or [])


def _pick_rules(rules: list[dict[str, Any]], many: bool) -> list[int] | None:
    if not rules:
        print_info("No firewall rule here")
        return None
    labels = [f"{r.get('pos')}: {describe_rule(r)}" for r in rules]
    if many:
        sel = multi_select_menu(labels, "  Rules:")
        if not sel:
            print_cancelled()
            return None
        return [rules[i]["pos"] for i in sel]
    idx = select_menu(labels, "  Rule:")
    if idx is None:
        print_cancelled()
        return None
    return [rules[idx]["pos"]]


def _check_positions(rules: list[dict[str, Any]], positions: list[int]) -> list[dict[str, Any]]:
    by_pos = {r.get("pos"): r for r in rules}
    missing = [p for p in positions if p not in by_pos]
    if missing:
        print_error(f"No rule at position {', '.join(map(str, missing))} (0 to {len(rules) - 1})"
                    if rules else "There is no rule here")
        raise typer.Exit(1)
    return [by_pos[p] for p in positions]


async def _interactive_rule(client: ProxmoxClient, scope: Scope, groups: list[str]) -> dict[str, Any] | None:
    """Ask for a rule step by step, when `rule add` gets no --action."""
    from ..utils import prompt
    from ..utils.menu import require_terminal

    require_terminal()
    choices = list(ACTIONS) + ([f"group {g}" for g in groups] if scope.kind != "group" else [])
    idx = select_menu(choices, "  Action:")
    if idx is None:
        return None
    action = choices[idx].removeprefix("group ")
    data: dict[str, Any] = {}
    if action in ACTIONS:
        allowed = scope.directions if scope.kind != "group" else ("in", "out", "forward")
        d = select_menu(list(allowed), "  Direction:") if len(allowed) > 1 else 0
        if d is None:
            return None
        data["type"] = allowed[d]
        macros = sorted(m.get("macro", "") for m in await client.get("/cluster/firewall/macros") or [])
        m = select_menu(["(none: protocol and ports)"] + macros, "  Macro:")
        if m is None:
            return None
        if m:
            data["macro"] = macros[m - 1]
        else:
            proto = prompt("  Protocol (tcp, udp, icmp; empty for any)", default="").strip()
            if proto:
                data["proto"] = proto
                if proto in ("tcp", "udp"):
                    port = prompt("  Destination port(s) (empty for any)", default="").strip()
                    if port:
                        data["dport"] = port
        data["action"] = action
    else:
        data["type"], data["action"] = "group", action
    for key, question in (("source", "Source (address, network, alias, +ipset; empty for any)"),
                          ("dest", "Destination (empty for any)"), ("comment", "Comment (empty for none)")):
        value = prompt(f"  {question}", default="").strip()
        if value:
            data[key] = value
    return data


def add_rule_commands(rule_app: typer.Typer, kind: str, target_arg: Any, drop_target: bool) -> None:
    """rule add/edit/remove/move/enable/disable for one kind of level."""
    from .firewall import _drop

    group_opt: Any = typer.Option(None, "--group", "-g", help="Rules of this security group") if kind == "cluster" else None
    # The datacenter has no target; only the datacenter has security groups
    sig = _drop("target") if drop_target else _drop("group")

    def scoped(scope: Scope, group: str | None) -> Scope:
        return group_scope(group) if group else scope

    async def guard_rules(client: ProxmoxClient, profile: str | None, scope: Scope, force: bool,
                          new: list[dict[str, Any]]) -> bool:
        """Refuse a rule change that closes the API to this machine.

        The rules of the datacenter, of a security group and of the node
        pvecli talks to come before the management hosts. Returns whether
        to check the access once the change is applied.
        """
        if scope.kind not in ("cluster", "group", "node"):
            return False

        profile_config = ConfigManager().get_profile(profile)
        if scope.kind == "node":
            # Unknown, the API node may be any node: guard them all
            api = await _api_node_or_none(client, profile_config.host)
            if api is not None and scope.name != api:
                return False

        def mutate(inputs: dict[str, Any]) -> None:
            if scope.kind == "group":
                inputs["groups"][scope.name] = new
            else:
                inputs["dc_rules" if scope.kind == "cluster" else "node_rules"] = new

        watch, _ = await guarded_change(client, profile_config, force, mutate)
        return watch

    async def checked(profile: str | None, scope: Scope) -> None:
        from .firewall_guard import check_after

        await check_after(ConfigManager().get_profile(profile), f"the change to the rules of {scope.label}")

    async def groups_check(client: ProxmoxClient, group: str | None) -> list[str]:
        groups = await _group_names(client)
        if group and group not in groups:
            print_error(f"Security group '{group}' not found")
            raise typer.Exit(1)
        return groups

    @rule_app.command("add")
    @sig
    @async_to_sync
    async def rule_add(
        target: str = target_arg,
        action: str = _OPT["action"],
        direction: str = _OPT["direction"],
        macro: str = _OPT["macro"],
        proto: str = _OPT["proto"],
        source: str = _OPT["source"],
        dest: str = _OPT["dest"],
        sport: str = _OPT["sport"],
        dport: str = _OPT["dport"],
        icmp_type: str = _OPT["icmp_type"],
        iface: str = _OPT["iface"],
        log: str = _OPT["log"],
        comment: str = _OPT["comment"],
        pos: int = typer.Option(0, "--pos", "-po", help="Position: 0 is evaluated first (the default, "
                                                          "like the web interface)"),
        disabled: bool = typer.Option(False, "--disabled", help="Add it disabled"),
        group: str = group_opt,
        force: bool = typer.Option(False, "--force", "-f", help="Go on even if the API would be closed "
                                                                "to this machine"),
        profile: str = PROFILE,
        yes: bool = YES,
    ) -> None:
        """Add a firewall rule (asked step by step without --action)."""

        async def body(client: ProxmoxClient, scope: Scope) -> None:
            groups = await groups_check(client, group)
            scope = scoped(scope, group)
            require_write(ConfigManager().get_profile(profile), profile, scope)
            if action is None:
                data = await _interactive_rule(client, scope, groups)
                if data is None:
                    print_cancelled()
                    return
            else:
                data = rule_fields(direction, action, macro, proto, source, dest, sport, dport,
                                   icmp_type, iface, log, comment, scope, groups)
                data.setdefault("type", scope.directions[0] if scope.kind != "group" else "in")
            rules = await read_rules(client, scope)
            if not 0 <= pos <= len(rules):
                print_error(f"Position {pos} is out of range: 0 to {len(rules)}")
                raise typer.Exit(1)
            data["enable"] = 0 if disabled else 1
            show_plan(f"Add to {scope.label} at position {pos}:", [describe_rule(data)])
            if pos < len(rules):
                console.print(f"  [dim]before: {describe_rule(rules[pos])}[/dim]", highlight=False)
            new = _renumber(rules[:pos] + [data] + rules[pos:])
            watch = await guard_rules(client, profile, scope, force, new)
            if not approved(yes):
                return
            await insert_rule(Writer(client, {scope.rules_path: rules}), scope, data, pos)
            print_success(f"Rule added to {scope.label} at position {pos}")
            if watch:
                await checked(profile, scope)

        try:
            await level_session(profile, kind, target, body)
        except PVECliError as e:
            _fail(e)

    @rule_app.command("edit")
    @sig
    @async_to_sync
    async def rule_edit(
        target: str = target_arg,
        position: int = typer.Argument(None, help="Position of the rule", metavar="pos"),
        action: str = _OPT["action"],
        direction: str = _OPT["direction"],
        macro: str = _OPT["macro"],
        proto: str = _OPT["proto"],
        source: str = _OPT["source"],
        dest: str = _OPT["dest"],
        sport: str = _OPT["sport"],
        dport: str = _OPT["dport"],
        icmp_type: str = _OPT["icmp_type"],
        iface: str = _OPT["iface"],
        log: str = _OPT["log"],
        comment: str = _OPT["comment"],
        clear: str = typer.Option(None, "--clear", help="Fields to empty, comma separated: "
                                                        + ", ".join(CLEARABLE)),
        group: str = group_opt,
        force: bool = typer.Option(False, "--force", "-f", help="Go on even if the API would be closed "
                                                                "to this machine"),
        profile: str = PROFILE,
        yes: bool = YES,
    ) -> None:
        """Change a firewall rule: only the options given change."""

        async def body(client: ProxmoxClient, scope: Scope) -> None:
            groups = await groups_check(client, group)
            scope = scoped(scope, group)
            require_write(ConfigManager().get_profile(profile), profile, scope)
            rules = await read_rules(client, scope)
            picked = [position] if position is not None else _pick_rules(rules, many=False)
            if picked is None:
                return
            (rule,) = _check_positions(rules, picked)
            data = rule_fields(direction, action, macro, proto, source, dest, sport, dport,
                               icmp_type, iface, log, comment, scope, groups)
            cleared = [c.strip() for c in (clear or "").split(",") if c.strip()]
            bad = [c for c in cleared if c not in CLEARABLE]
            if bad:
                print_error(f"Cannot clear {', '.join(bad)}: {', '.join(CLEARABLE)}")
                raise typer.Exit(1)
            if not data and not cleared:
                print_error("Nothing to change: pass the options to change, or --clear")
                raise typer.Exit(1)
            after = {k: v for k, v in {**rule, **data}.items() if k not in cleared}
            show_plan(f"Change rule {rule['pos']} of {scope.label}:",
                      [f"from: {describe_rule(rule)}", f"to:   {describe_rule(after)}"])
            watch = await guard_rules(client, profile, scope, force,
                                      [after if r is rule else r for r in rules])
            if not approved(yes):
                return
            if cleared:
                data["delete"] = ",".join(cleared)
            await Writer(client, {scope.rules_path: rules}).put(f"{scope.rules_path}/{rule['pos']}", data)
            print_success(f"Rule {rule['pos']} of {scope.label} changed")
            if watch:
                await checked(profile, scope)

        try:
            await level_session(profile, kind, target, body)
        except PVECliError as e:
            _fail(e)

    @rule_app.command("remove")
    @sig
    @async_to_sync
    async def rule_remove(
        target: str = target_arg,
        positions: list[int] = typer.Argument(None, help="Positions of the rules", metavar="pos..."),
        group: str = group_opt,
        force: bool = typer.Option(False, "--force", "-f", help="Go on even if the API would be closed "
                                                                "to this machine"),
        profile: str = PROFILE,
        yes: bool = YES,
    ) -> None:
        """Remove firewall rules."""

        async def body(client: ProxmoxClient, scope: Scope) -> None:
            await groups_check(client, group)
            scope = scoped(scope, group)
            require_write(ConfigManager().get_profile(profile), profile, scope)
            rules = await read_rules(client, scope)
            picked = list(positions) if positions else _pick_rules(rules, many=True)
            if picked is None:
                return
            chosen = _check_positions(rules, sorted(set(picked)))
            show_plan(f"Remove from {scope.label}:", [f"rule {r['pos']}: {describe_rule(r)}" for r in chosen])
            watch = await guard_rules(client, profile, scope, force,
                                      _renumber([r for r in rules if r not in chosen]))
            if not approved(yes):
                return
            w = Writer(client, {scope.rules_path: rules})
            # The last one first: removing a rule moves up every rule after it
            for r in sorted(chosen, key=lambda x: x["pos"], reverse=True):
                await w.delete(f"{scope.rules_path}/{r['pos']}")
            print_success(f"{len(chosen)} rule{'' if len(chosen) == 1 else 's'} removed from {scope.label}")
            if watch:
                await checked(profile, scope)

        try:
            await level_session(profile, kind, target, body)
        except PVECliError as e:
            _fail(e)

    @rule_app.command("move")
    @sig
    @async_to_sync
    async def rule_move(
        target: str = target_arg,
        position: int = typer.Argument(..., help="Position of the rule", metavar="pos"),
        to: int = typer.Argument(..., help="Its new position", metavar="new-pos"),
        group: str = group_opt,
        force: bool = typer.Option(False, "--force", "-f", help="Go on even if the API would be closed "
                                                                "to this machine"),
        profile: str = PROFILE,
        yes: bool = YES,
    ) -> None:
        """Move a firewall rule: rules are evaluated from position 0 down."""

        async def body(client: ProxmoxClient, scope: Scope) -> None:
            await groups_check(client, group)
            scope = scoped(scope, group)
            require_write(ConfigManager().get_profile(profile), profile, scope)
            rules = await read_rules(client, scope)
            (rule,) = _check_positions(rules, [position])
            if not 0 <= to < len(rules):
                print_error(f"New position {to} is out of range: 0 to {len(rules) - 1}")
                raise typer.Exit(1)
            if to == position:
                print_info(f"Rule {position} is already at position {to}")
                return
            show_plan(f"Move in {scope.label}:", [f"rule {position} -> {to}: {describe_rule(rule)}"])
            moved = [r for r in rules if r is not rule]
            moved.insert(to, rule)
            watch = await guard_rules(client, profile, scope, force, _renumber(moved))
            if not approved(yes):
                return
            await Writer(client, {scope.rules_path: rules}).put(f"{scope.rules_path}/{position}", {"moveto": _moveto(position, to)})
            print_success(f"Rule moved to position {to} in {scope.label}")
            if watch:
                await checked(profile, scope)

        try:
            await level_session(profile, kind, target, body)
        except PVECliError as e:
            _fail(e)

    def switch(enable: bool) -> Any:
        verb = "enable" if enable else "disable"

        @rule_app.command(verb)
        @sig
        @async_to_sync
        async def rule_switch(
            target: str = target_arg,
            positions: list[int] = typer.Argument(None, help="Positions of the rules", metavar="pos..."),
            group: str = group_opt,
            force: bool = typer.Option(False, "--force", "-f", help="Go on even if the API would be closed "
                                                                    "to this machine"),
            profile: str = PROFILE,
            yes: bool = YES,
        ) -> None:
            async def body(client: ProxmoxClient, scope: Scope) -> None:
                await groups_check(client, group)
                scope = scoped(scope, group)
                require_write(ConfigManager().get_profile(profile), profile, scope)
                rules = await read_rules(client, scope)
                picked = list(positions) if positions else _pick_rules(rules, many=True)
                if picked is None:
                    return
                chosen = [r for r in _check_positions(rules, sorted(set(picked)))
                          if bool(int(r.get("enable", 1) or 0)) != enable]
                if not chosen:
                    print_info(f"Already {verb}d")
                    return
                show_plan(f"{verb.capitalize()} in {scope.label}:",
                          [f"rule {r['pos']}: {describe_rule(r)}" for r in chosen])
                watch = await guard_rules(client, profile, scope, force,
                                          [{**r, "enable": 1 if enable else 0} if r in chosen else r for r in rules])
                if not approved(yes):
                    return
                w = Writer(client, {scope.rules_path: rules})
                for r in chosen:
                    await w.put(f"{scope.rules_path}/{r['pos']}", {"enable": 1 if enable else 0})
                print_success(f"{len(chosen)} rule{'' if len(chosen) == 1 else 's'} {verb}d in {scope.label}")
                if watch:
                    await checked(profile, scope)

            try:
                await level_session(profile, kind, target, body)
            except PVECliError as e:
                _fail(e)

        rule_switch.__doc__ = f"{verb.capitalize()} firewall rules without removing them."
        return rule_switch

    switches = [switch(True), switch(False)]
    # Only the datacenter (and its groups) and the nodes come before the
    # management hosts: elsewhere a rule cannot close the API
    if kind not in ("cluster", "node"):
        for fn in (rule_add, rule_edit, rule_remove, rule_move, *switches):
            _strip(fn, "force")


def _renumber(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The positions a list of rules takes once written."""
    return [{**r, "pos": i} for i, r in enumerate(rules)]


def _moveto(position: int, to: int) -> int:
    """The moveto Proxmox wants for a rule to land at position `to`.

    Proxmox puts the rule before the one at `moveto`, counted before the
    move: going down, that is one further (checked on PVE 9.1).
    """
    return to + 1 if to > position else to


async def insert_rule(w: Writer, scope: Scope, data: dict[str, Any], pos: int) -> None:
    """Create a rule at position `pos`.

    Proxmox ignores `pos` on creation and puts every new rule first. A rule
    meant further down is created disabled, moved, then enabled: it never
    acts from the wrong place, even for the instant between two writes.
    """
    if pos == 0:
        await w.post(scope.rules_path, data)
        return
    await w.post(scope.rules_path, {**data, "enable": 0})
    await w.put(f"{scope.rules_path}/0", {"moveto": _moveto(0, pos)})
    if data.get("enable", 1):
        await w.put(f"{scope.rules_path}/{pos}", {"enable": 1})


# ── switches and options ─────────────────────────────────────────────────

_LEVELS = tuple(LOG_LEVELS)
_POLICY = ("ACCEPT", "REJECT", "DROP")
# What `set` accepts on each level, from the PVE 9.1 schema
OPTION_TYPES: dict[str, dict[str, Any]] = {
    "cluster": {"enable": bool, "policy_in": _POLICY, "policy_out": _POLICY,
                "policy_forward": ("ACCEPT", "DROP"), "ebtables": bool, "log_ratelimit": str},
    "node": {"enable": bool, "log_level_in": _LEVELS, "log_level_out": _LEVELS, "log_level_forward": _LEVELS,
             "log_nf_conntrack": bool, "ndp": bool, "nf_conntrack_allow_invalid": bool,
             "nf_conntrack_helpers": str, "nf_conntrack_max": int, "nf_conntrack_tcp_timeout_established": int,
             "nf_conntrack_tcp_timeout_syn_recv": int, "nftables": bool, "nosmurfs": bool,
             "protection_synflood": bool, "protection_synflood_burst": int, "protection_synflood_rate": int,
             "smurf_log_level": _LEVELS, "tcp_flags_log_level": _LEVELS, "tcpflags": bool},
    "vm": {"enable": bool, "policy_in": _POLICY, "policy_out": _POLICY, "dhcp": bool, "ipfilter": bool,
           "macfilter": bool, "ndp": bool, "radv": bool, "log_level_in": _LEVELS, "log_level_out": _LEVELS},
    "vnet": {"enable": bool, "policy_forward": ("ACCEPT", "DROP"), "log_level_forward": _LEVELS},
}
OPTION_TYPES["ct"] = OPTION_TYPES["vm"]


def parse_settings(kind: str, pairs: list[str]) -> dict[str, Any]:
    """KEY=VALUE pairs, checked against what the level accepts."""
    allowed = OPTION_TYPES[kind]
    out: dict[str, Any] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        key = key.strip().replace("-", "_")
        if not sep or key not in allowed:
            print_error(f"Not an option of this level: '{pair}'. One of: {', '.join(sorted(allowed))}")
            raise typer.Exit(1)
        kind_of = allowed[key]
        value = value.strip()
        if kind_of is bool:
            if value.lower() not in ("1", "0", "yes", "no", "on", "off", "true", "false"):
                print_error(f"{key} takes yes or no, not '{value}'")
                raise typer.Exit(1)
            out[key] = 1 if value.lower() in ("1", "yes", "on", "true") else 0
        elif kind_of is int:
            if not value.isdigit():
                print_error(f"{key} takes a number, not '{value}'")
                raise typer.Exit(1)
            out[key] = int(value)
        elif isinstance(kind_of, tuple):
            match = next((v for v in kind_of if v.lower() == value.lower()), None)
            if match is None:
                print_error(f"{key} takes one of {', '.join(kind_of)}, not '{value}'")
                raise typer.Exit(1)
            out[key] = match
        else:
            out[key] = value
    return out


async def guarded_change(client: ProxmoxClient, profile_config: Any, force: bool,
                         mutate: Any) -> tuple[bool, Any]:
    """Judge the access to the API before and after a change.

    mutate(inputs) changes, in place, the configuration read by
    read_access_inputs the way the change will. Refuses (exit 1) a change
    that closes the API, unless force. Returns (watch, after): watch is
    True when the firewall will filter the API after the change, so the
    access must be checked once it is applied.
    """
    import copy
    import socket

    from .firewall_guard import api_node, check_access, judge, read_access_inputs, source_address

    try:
        node = await api_node(client, profile_config.host)
        inputs = await read_access_inputs(client, node) if node else None
    except PVECliError as e:
        node, inputs = None, None
        print_warning(f"pvecli cannot read what decides its access to the API ({e})")
    if node is None or inputs is None:
        # Not checked before: checked after, which needs no reading
        print_warning("The access to the API is not checked before the change, only after it "
                      "(with how to get back in if it is lost)")
        return True, None
    ip = source_address(profile_config.host, profile_config.port)
    # A rule's dest is matched against the address pvecli connects to, which
    # can be another interface than the cluster address of the node
    try:
        inputs["node_ip"] = socket.gethostbyname(profile_config.host)
    except OSError:
        pass
    after_inputs = copy.deepcopy(inputs)
    mutate(after_inputs)
    before, after = judge(ip, **inputs), judge(ip, **after_inputs)
    if before.kept is False and after.kept is False:
        print_warning(f"This machine is already outside the management hosts: {after.reason}")
    else:
        check_access(after, force)
    filtered = not (after.reason.startswith(("the datacenter firewall is off", "the firewall of the node"))
                    or "input policy is ACCEPT" in after.reason)
    return filtered, after


async def _api_node_or_none(client: ProxmoxClient, host: str) -> str | None:
    """The node pvecli talks to, None when it cannot tell (or not read it)."""
    from .firewall_guard import api_node

    try:
        return await api_node(client, host)
    except PVECliError:
        return None


def add_switch_commands(app: typer.Typer, kind: str, target_arg: Any, drop: Any) -> None:
    """enable, disable and set for one kind of level."""
    from .firewall import effective, is_on, read_options

    def run(profile: str | None, target: str | None, body: Any) -> Any:
        return level_session(profile, kind, target, body)

    async def apply(client: ProxmoxClient, scope: Scope, profile: str | None, data: dict[str, Any],
                    lines: list[str], force: bool, yes: bool) -> None:
        profile_config = ConfigManager().get_profile(profile)
        before = await client.get(scope.options_path) or {}

        show_plan(f"Change the firewall of {scope.label}:", lines)
        watch = False
        if scope.kind in ("cluster", "node"):

            api = await _api_node_or_none(client, profile_config.host)
            # The options of the datacenter, or of the node pvecli talks to,
            # decide whether it keeps the API
            key = "dc_opts" if scope.kind == "cluster" else "node_opts"
            if scope.kind == "cluster" or api is None or scope.name == api:

                def mutate(inputs: dict[str, Any]) -> None:
                    opts = inputs[key]
                    for k in str(data.get("delete", "")).split(","):
                        opts.pop(k.strip(), None)
                    opts.update({k: v for k, v in data.items() if k != "delete"})

                watch, _ = await guarded_change(client, profile_config, force, mutate)
        if not approved(yes):
            return
        w = Writer(client, {scope.options_path: before})
        await w.put(scope.options_path, data)
        print_success(f"Firewall of {scope.label} changed")
        if watch:
            from .firewall_guard import check_after

            await check_after(profile_config, f"the change to the firewall of {scope.label}")

    def describe(scope: Scope, opts: dict[str, Any], data: dict[str, Any]) -> list[str]:
        lines = []
        def shown(value: Any) -> str:
            return "-" if value is None else str(value)

        for k, v in data.items():
            if k == "delete":
                for key in v.split(","):
                    default = effective(scope.kind, {}, key)
                    lines.append(f"{key}: {shown(opts.get(key))} -> default"
                                 + (f" ({default})" if default is not None else ""))
                continue
            lines.append(f"{k}: {shown(effective(scope.kind, opts, k))} -> {v}")
        return lines

    def switch(enable: bool) -> None:
        verb = "enable" if enable else "disable"

        @app.command(verb)
        @drop
        @async_to_sync
        async def toggle(
            target: str = target_arg,
            nics: bool = typer.Option(False, "--nics", help="Also set firewall=1 on every card of the guest "
                                                            "(a running guest loses its network briefly)")
            if kind in ("vm", "ct") and enable else None,  # type: ignore[assignment]
            force: bool = typer.Option(False, "--force", "-f", help="Go on even if the API would be closed "
                                                                    "to this machine")
            if kind in ("cluster", "node") and enable else None,  # type: ignore[assignment]
            profile: str = PROFILE,
            yes: bool = YES,
        ) -> None:
            async def body(client: ProxmoxClient, scope: Scope) -> None:
                if scope.is_guest and enable:
                    await _enable_guest(client, scope, bool(nics), yes)
                    return
                opts = await read_options(client, scope)
                if is_on(effective(scope.kind, opts, "enable")) == enable:
                    print_info(f"The firewall of {scope.label} is already {'on' if enable else 'off'}")
                    return
                await apply(client, scope, profile, {"enable": 1 if enable else 0},
                            [f"firewall: {'off -> on' if enable else 'on -> off'}"], bool(force), yes)

            try:
                await run(profile, target, body)
            except PVECliError as e:
                _fail(e)

        toggle.__doc__ = (f"Turn the firewall of {_noun(kind)} {'on' if enable else 'off'}."
                          + (" Checks the API stays reachable." if kind in ("cluster", "node") and enable else ""))
        if not (kind in ("vm", "ct") and enable):
            _strip(toggle, "nics")
        if not (kind in ("cluster", "node") and enable):
            _strip(toggle, "force")

    switch(True)
    switch(False)

    @app.command("set")
    @drop
    @async_to_sync
    async def set_options(
        target: str = target_arg,
        settings: list[str] = typer.Argument(None, help="KEY=VALUE pairs, as `options` names them",
                                             metavar="key=value..."),
        clear: str = typer.Option(None, "--clear", help="Options back to their default, comma separated"),
        force: bool = typer.Option(False, "--force", "-f", help="Go on even if the API would be closed "
                                                                "to this machine")
        if kind in ("cluster", "node") else None,  # type: ignore[assignment]
        profile: str = PROFILE,
        yes: bool = YES,
    ) -> None:
        """Change firewall options: policies, log levels, filters."""
        data = parse_settings(kind, list(settings or []))
        cleared = [c.strip().replace("-", "_") for c in (clear or "").split(",") if c.strip()]
        bad = [c for c in cleared if c not in OPTION_TYPES[kind]]
        if bad:
            print_error(f"Not an option of this level: {', '.join(bad)}")
            raise typer.Exit(1)
        if not data and not cleared:
            print_error("Nothing to change: pass KEY=VALUE pairs or --clear")
            raise typer.Exit(1)
        if cleared:
            data["delete"] = ",".join(cleared)

        async def body(client: ProxmoxClient, scope: Scope) -> None:
            opts = await read_options(client, scope)
            await apply(client, scope, profile, data, describe(scope, opts, data), bool(force), yes)

        try:
            await run(profile, target, body)
        except PVECliError as e:
            _fail(e)

    if kind not in ("cluster", "node"):
        _strip(set_options, "force")


def _noun(kind: str) -> str:
    return {"cluster": "the datacenter", "vm": "a VM", "ct": "a container", "node": "a node", "vnet": "a vnet"}[kind]


def _strip(fn: Any, name: str) -> None:
    import inspect

    sig = inspect.signature(fn)
    fn.__signature__ = sig.replace(parameters=[p for n, p in sig.parameters.items() if n != name])


async def _enable_guest(client: ProxmoxClient, scope: Scope, nics: bool, yes: bool) -> None:
    """Turn the firewall of a guest on and, with --nics, flag its cards: one plan, one question.

    A card without firewall=1 is not filtered, whatever the rules say.
    """
    from .firewall import effective, guest_nics, is_on

    assert scope.node is not None and scope.vmid is not None
    api = "qemu" if scope.kind == "vm" else "lxc"
    before = await client.get(scope.options_path) or {}
    on = is_on(effective(scope.kind, before, "enable"))
    config = await client.get(f"/nodes/{scope.node}/{api}/{scope.vmid}/config") or {}
    unflagged = [n for n, flagged in guest_nics(config).items() if not flagged]
    cards = unflagged if nics else []
    if on:
        print_info(f"The firewall of {scope.label} is already on")
    lines = ([] if on else ["firewall: off -> on"]) + [f"{n}: firewall=1" for n in cards]
    if lines:
        show_plan(f"Change the firewall of {scope.label}:", lines)
        if cards:
            status = await client.get(f"/nodes/{scope.node}/{api}/{scope.vmid}/status/current") or {}
            if status.get("status") == "running":
                console.print("  [yellow]The guest is running: each card is plugged again, its network "
                              "drops for a moment.[/yellow]")
        if not approved(yes):
            return
        if not on:
            await Writer(client, {scope.options_path: before}).put(scope.options_path, {"enable": 1})
        if cards:
            data = {n: _with_firewall(config[n]) for n in cards}
            await client.put(f"/nodes/{scope.node}/{api}/{scope.vmid}/config",
                             data={**data, "digest": config.get("digest")})
        print_success(f"Firewall of {scope.label} changed")
    if unflagged and not nics:
        print_warning(f"Not filtered: {', '.join(unflagged)} (no firewall=1). "
                      f"Pass --nics to flag them, or edit the guest.")


def _with_firewall(card: str) -> str:
    parts = [p for p in str(card).split(",") if not p.startswith("firewall=")]
    return ",".join(parts + ["firewall=1"])
