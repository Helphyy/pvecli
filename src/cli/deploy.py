"""Deploy many VMs and containers from a YAML file, on one or several clusters.

  pvecli deploy lab.yaml --dry-run
  pvecli deploy lab.yaml -y

The file holds `defaults` and a list of `guests` (see examples/deploy.yaml).
Each guest goes through the same steps as vm deploy or ct deploy. The whole
file is checked first, on every cluster, before the first write; a guest
whose name already exists on its cluster is skipped, never overwritten, so
running the file twice creates nothing the second time.
"""

import asyncio
import ipaddress
import re
from collections import defaultdict
from collections.abc import Callable, Hashable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.markup import escape
from rich.table import Table

from ..api.client import ProxmoxClient
from ..api.exceptions import PVECliError
from ..config import ConfigManager
from ..utils import confirm, console, print_cancelled, print_error, print_info, print_warning
from ..utils.fleet import _cluster_identity, is_multi_profile
from .cloudinit import ClusterState, Facts, GuestSpec, Outcome, create_vm, prepare_vm, validate_vm
from .ct_deploy import create_ct, prepare_ct, validate_ct

# Keys of a guest entry, then those only one type takes
_COMMON = {
    "type", "name", "count", "start", "profile", "id", "node", "template", "storage", "full", "cores", "memory",
    "disk_size", "tags", "pool", "ip", "gw", "ip6", "gw6", "dns", "search_domain", "ssh_key", "password_env",
    "power_on",
}
_ONLY = {"vm": {"user", "upgrade"}, "ct": {"swap", "bridge", "vlan", "unprivileged", "features"}}
# What makes one guest itself: meaningless as a default
_PER_GUEST = {"name", "count", "start", "id"}
_DEFAULT_KEYS = (_COMMON - _PER_GUEST) | _ONLY["vm"] | _ONLY["ct"]

_INT = {"count", "start", "id", "cores", "vlan"}
_BOOL = {"full", "upgrade", "unprivileged", "power_on"}
_SIZES = {"memory", "swap", "disk_size"}
_LISTS = {"tags", "ssh_key"}
# File key -> GuestSpec field, where they differ
_FIELD = {"id": "vmid", "ssh_key": "ssh_keys", "power_on": "start"}
_PASSWORD = "never put a password in the file: use password_env: NAME_OF_AN_ENV_VAR"
# null clears a default for these (back to PVE's own default); the others
# always have a value
_NULLABLE = (_COMMON | _ONLY["vm"] | _ONLY["ct"]) - {"type", "name", "count", "start", "template", "full", "power_on"}


def _coerce(key: str, value: Any) -> Any:
    """A YAML value as the spec wants it. Raises ValueError with the reason."""
    if value is None:
        if key not in _NULLABLE:
            raise ValueError(f"'{key}' needs a value")
        return [] if key in _LISTS else None
    if key in _INT:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"'{key}' must be an integer, got {value!r}")
        return value
    if key in _BOOL:
        if not isinstance(value, bool):
            raise ValueError(f"'{key}' must be true or false, got {value!r}")
        return value
    if key in _SIZES:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError(f"'{key}' must be a size like 2G, got {value!r}")
        # A bare number: GiB for a disk, MiB for memory (as PVE counts them)
        return f"{value}G" if key == "disk_size" and isinstance(value, int) else str(value)
    if key in _LISTS:
        items = value.split(",") if isinstance(value, str) and key == "tags" else value
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, list) or not all(isinstance(x, (str, int)) for x in items):
            raise ValueError(f"'{key}' must be a string or a list of strings")
        return [str(x).strip() for x in items if str(x).strip()]
    if key == "features" and isinstance(value, dict):
        return ",".join(f"{k}={int(v) if isinstance(v, bool) else v}" for k, v in value.items())
    if key == "template" and isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, (dict, list)) or value is None:
        raise ValueError(f"'{key}' must be a single value")
    return str(value)


def _check_keys(where: str, entry: dict[str, Any], allowed: set[str], errors: list[str]) -> dict[str, Any]:
    """The valid keys of a mapping, coerced; an error for each other one."""
    out: dict[str, Any] = {}
    for key, value in entry.items():
        if key == "password":
            errors.append(f"{where}: {_PASSWORD}")
        elif key not in allowed:
            errors.append(f"{where}: unknown key '{key}'")
        else:
            try:
                out[key] = _coerce(key, value)
            except ValueError as e:
                errors.append(f"{where}: {e}")
    return out


def series_address(addr: str, offset: int) -> str:
    """The address of guest number `offset` of a series: dhcp stays dhcp,
    a static one steps by one and must stay inside its subnet."""
    if "/" not in addr:
        return addr
    try:
        iface = ipaddress.ip_interface(addr)
    except ValueError:
        return addr  # prepare reports it, with the option it came from
    net = iface.network
    try:
        ip = iface.ip + offset
    except ipaddress.AddressValueError:
        ip = None
    edges = (net.network_address, net.broadcast_address) if net.version == 4 and net.prefixlen < 31 else ()
    if ip is None or ip not in net or ip in edges:
        raise PVECliError(f"the series from {addr} leaves {net} at its guest number {offset + 1}")
    return f"{ip}/{net.prefixlen}"


def parse_document(doc: Any, cli_profile: str | None, fallback_profile: str | None) -> tuple[list[GuestSpec], list[str]]:
    """The guests a file describes, series expanded, and every schema error."""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return [], ["The file must be a mapping with 'guests' (and optionally 'defaults')"]
    for key in doc:
        if key not in ("defaults", "guests"):
            errors.append(f"Unknown top-level key '{key}' (expected: defaults, guests)")

    raw_defaults = doc.get("defaults") or {}
    if not isinstance(raw_defaults, dict):
        errors.append("defaults: must be a mapping")
        raw_defaults = {}
    common = _check_keys("defaults", {k: v for k, v in raw_defaults.items() if k not in _ONLY}, _DEFAULT_KEYS, errors)
    per_type: dict[str, dict[str, Any]] = {}
    for kind in _ONLY:
        section = raw_defaults.get(kind) or {}
        if not isinstance(section, dict):
            errors.append(f"defaults.{kind}: must be a mapping")
            section = {}
        other = _ONLY["ct" if kind == "vm" else "vm"]
        per_type[kind] = _check_keys(f"defaults.{kind}", section, _DEFAULT_KEYS - other, errors)

    guests = doc.get("guests")
    if not isinstance(guests, list) or not guests:
        return [], errors + ["'guests' must be a non-empty list"]

    specs: list[GuestSpec] = []
    for i, entry in enumerate(guests):
        label = f"guests[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{label}: must be a mapping")
            continue
        if isinstance(entry.get("name"), str):
            label += f" ({entry['name']})"
        # A refused key is reported and left out; the rest of the entry is
        # still checked, so one run lists every problem of the file
        own = _check_keys(label, entry, _COMMON | _ONLY["vm"] | _ONLY["ct"], errors)
        given = own.get("type", common.get("type"))
        if given not in _ONLY:
            if "type" not in entry or "type" in own:
                errors.append(f"{label}: type must be 'vm' or 'ct'" + (f", got '{given}'" if given else ", it is missing"))
            continue
        kind = str(given)
        other = _ONLY["ct" if kind == "vm" else "vm"]
        for key in sorted(own.keys() & other):
            errors.append(f"{label}: key '{key}' does not apply to a {kind}")
            del own[key]
        # Defaults of the other type are not this guest's business
        merged = {**{k: v for k, v in common.items() if k not in other}, **per_type[kind], **own}
        # Without these the guest cannot be built; one already refused is
        # not reported a second time as missing
        needed = ("name", "template", "count", "start")
        if any(k in entry and k not in own for k in needed):
            continue
        missing = [k for k in ("name", "template") if k not in merged]
        if missing:
            errors += [f"{label}: {k} is missing" for k in missing]
            continue
        specs += _expand(label, i, kind, merged, cli_profile or fallback_profile, errors)
    return specs, errors


def _expand(label: str, index: int, kind: str, entry: dict[str, Any], profile: str | None,
            errors: list[str]) -> list[GuestSpec]:
    count, first, pattern = entry.get("count", 1), entry.get("start", 1), entry["name"]
    if count < 1:
        errors.append(f"{label}: count must be 1 or more")
        return []
    if count > 1 and "{n" not in pattern:
        errors.append(f"{label}: a series (count: {count}) needs {{n}} in its name, e.g. web{{n}}")
        return []
    fields = {_FIELD.get(k, k): v for k, v in entry.items() if k not in ("type", "name", "count", "start", "id")}
    out = []
    for j in range(count):
        try:
            name = pattern.format(n=first + j)
            ips: dict[str, Any] = {k: series_address(entry[k], j) for k in ("ip", "ip6") if k in entry}
        except (KeyError, IndexError, ValueError) as e:
            errors.append(f"{label}: invalid name pattern '{pattern}' ({e})")
            return []
        except PVECliError as e:
            errors.append(f"{label}: {e}")
            return []
        values: dict[str, Any] = {**fields, **ips}
        spec = GuestSpec(kind=kind, name=name, **values)
        spec.profile = spec.profile or profile
        spec.vmid = entry["id"] + j if "id" in entry else None
        spec.label = f"guests[{index}] ({name})"
        out.append(spec)
    return out


def file_duplicates(specs: list[GuestSpec], cluster_of: Callable[[str | None], Hashable]) -> list[str]:
    """Two guests of the file on one cluster with the same name or ID, or
    anywhere with the same static address."""
    names: dict[Any, list[str]] = defaultdict(list)
    ids: dict[Any, list[str]] = defaultdict(list)
    addrs: dict[str, list[str]] = defaultdict(list)
    for s in specs:
        cluster = cluster_of(s.profile)
        names[(cluster, s.name)].append(s.label)
        if s.vmid is not None:
            ids[(cluster, s.vmid)].append(s.label)
        for addr in (s.ip, s.ip6):
            if addr and "/" in addr:
                try:
                    addrs[str(ipaddress.ip_interface(addr).ip)].append(s.label)
                except ValueError:
                    pass
    errors = [f"Name '{name}' is given {len(where)} times on one cluster: {', '.join(where)}"
              for (_, name), where in names.items() if len(where) > 1]
    errors += [f"ID {vmid} is given {len(where)} times on one cluster: {', '.join(where)}"
               for (_, vmid), where in ids.items() if len(where) > 1]
    errors += [f"Address {addr} is given {len(where)} times: {', '.join(where)}"
               for addr, where in addrs.items() if len(where) > 1]
    return errors


@dataclass
class _Row:
    spec: GuestSpec
    action: str  # create, present, conflict
    note: str = ""
    existing: dict[str, Any] | None = None


async def _state(facts: Facts, spec: GuestSpec) -> _Row:
    """Whether the guest is to create, already there (same name), or in the
    way of something else."""
    kind = "qemu" if spec.kind == "vm" else "lxc"
    guests = await facts.guests()
    same = next((g for g in guests if g.get("name") == spec.name), None)
    if same:
        if same.get("type") != kind:
            other = "a VM" if same.get("type") == "qemu" else "a CT"
            return _Row(spec, "conflict", f"{other} named {spec.name} exists ({same.get('vmid')})", same)
        return _Row(spec, "present", f"{spec.what} {same.get('vmid')} on {same.get('node')}", same)
    if spec.vmid is not None:
        holder = next((g for g in guests if g.get("vmid") == spec.vmid), None)
        if holder or not await facts.id_free(spec.vmid):
            return _Row(spec, "conflict", f"ID {spec.vmid} is taken" + (f" by {holder.get('name')}" if holder else ""))
    return _Row(spec, "create")


def _cluster_label(ident: Any, profile: str) -> str:
    if isinstance(ident, tuple) and ident and ident[0] == "cluster":
        return f"cluster {ident[1]}"
    if isinstance(ident, tuple) and ident:
        return f"node {', '.join(ident[1])}"
    return f"profile {profile}"


def _network(spec: GuestSpec) -> str:
    return ", ".join(x for x in (spec.ip, spec.ip6) if x) or "-"


def _print_plan(rows: list[_Row], label_of: dict[str | None, str]) -> None:
    style = {"create": "green", "present": "dim", "conflict": "bold red"}
    by_cluster: dict[str, list[_Row]] = defaultdict(list)
    for r in rows:
        by_cluster[label_of[r.spec.profile]].append(r)
    for cluster, group in by_cluster.items():
        profiles = ", ".join(dict.fromkeys(str(r.spec.profile) for r in group))
        table = Table(title=f"{cluster} (profile {profiles})", header_style="bold cyan", title_justify="left")
        for col in ("Action", "Type", "Name", "ID", "Node", "Source", "Network", "Note"):
            table.add_column(col, overflow="fold")
        for r in group:
            s, ex = r.spec, r.existing or {}
            node = ex.get("node") if r.action == "present" else s.src.get("target") or s.node
            vmid = ex.get("vmid") if r.action == "present" else s.vmid
            table.add_row(f"[{style[r.action]}]{r.action}[/{style[r.action]}]", s.what, escape(s.name or ""),
                          str(vmid or "-"), escape(str(node or "-")),
                          # the file name says it; the storage:vztmpl/ prefix only widens the table
                          escape(s.template.rsplit("/", 1)[-1] if r.action != "present" else "-"), escape(_network(s)),
                          escape(r.note))
        console.print(table)


def _print_summary(rows: list[_Row], results: dict[int, Outcome], label_of: dict[str | None, str]) -> int:
    table = Table(title="Result", header_style="bold cyan", title_justify="left")
    for col in ("Name", "Type", "Cluster", "ID", "Node", "Result", "IP", "Error"):
        table.add_column(col, overflow="fold")
    failed, leftovers = 0, []
    for r in rows:
        s, ex = r.spec, r.existing or {}
        out = results.get(id(s))
        if out is None:
            table.add_row(escape(s.name or ""), s.what, escape(label_of[s.profile]), str(ex.get("vmid", "-")),
                          escape(str(ex.get("node", "-"))), "[dim]skipped (present)[/dim]", "", "")
            continue
        if out.error:
            failed += 1
            if out.created:
                cmd = "vm" if s.kind == "vm" else "ct"
                leftovers.append(f"{s.what} {out.vmid} ({s.name}) on {label_of[s.profile]}: "
                                 f"pvecli {cmd} remove {out.vmid} -p {s.profile}")
        result = "[bold red]failed[/bold red]" if out.error else "[green]created[/green]"
        table.add_row(escape(s.name or ""), s.what, escape(label_of[s.profile]), str(out.vmid or "-"),
                      escape(str(out.node or "-")), result, escape(out.ip or ""), escape(out.error or ""))
    console.print(table)
    if leftovers:
        print_warning("Failed after its creation started, left as is (nothing is deleted automatically):")
        for line in leftovers:
            console.print(f"  {escape(line)}", highlight=False)
    if failed:
        print_error(f"{failed} guest(s) failed")
    return 1 if failed else 0


def _file_order(problem: str) -> int:
    m = re.match(r"guests\[(\d+)\]", problem)
    return int(m.group(1)) if m else -1


async def _execute(rows: list[_Row], facts: dict[str | None, Facts], parallel: int) -> dict[int, Outcome]:
    sem = asyncio.Semaphore(max(1, parallel))

    async def one(spec: GuestSpec) -> tuple[int, Outcome]:
        out = Outcome(vmid=spec.vmid)

        def say(level: str, msg: str) -> None:
            color = {"ok": "green", "warn": "yellow"}.get(level, "cyan")
            console.print(f"[bold]{escape(spec.name or '')}[/bold]: [{color}]{escape(msg)}[/{color}]", highlight=False)

        async with sem:
            try:
                create = create_vm if spec.kind == "vm" else create_ct
                await create(facts[spec.profile], spec, say, out)
            except (PVECliError, ValueError) as e:
                out.error = str(e)
                console.print(f"[bold]{escape(spec.name or '')}[/bold]: [bold red]failed:[/bold red] {escape(str(e))}",
                              highlight=False)
        return id(spec), out

    return dict(await asyncio.gather(*(one(r.spec) for r in rows)))


async def _run(doc: Any, cli_profile: str | None, dry_run: bool, yes: bool, parallel: int, wait_ip: int) -> int:
    config = ConfigManager()
    specs, errors = parse_document(doc, cli_profile, config.get().default_profile)
    unprepared = set()
    for s in specs:
        s.wait_ip = wait_ip
        problems = (prepare_vm if s.kind == "vm" else prepare_ct)(s)
        if problems:
            unprepared.add(id(s))
            errors += [f"{s.label}: {p}" for p in problems]
        if not s.profile:
            unprepared.add(id(s))
            errors.append(f"{s.label}: no profile: pass -p, set profile: in the file, or a default profile")

    async with AsyncExitStack() as stack:
        clients: dict[str | None, ProxmoxClient] = {}
        for p in dict.fromkeys(s.profile for s in specs if s.profile):
            try:
                clients[p] = await stack.enter_async_context(ProxmoxClient(config.get_profile(p)))
            except PVECliError as e:
                errors.append(f"Profile {p}: {e}")
        # Two profiles on one cluster share its names, IDs and clone locks
        idents = dict(zip(clients, await asyncio.gather(
            *(asyncio.to_thread(_cluster_identity, str(p)) for p in clients)), strict=True))
        states: dict[Any, ClusterState] = {}
        facts: dict[str | None, Facts] = {}
        label_of: dict[str | None, str] = {}
        for name, client in clients.items():
            ident = idents[name]
            if isinstance(ident, str):
                errors.append(f"Profile {name}: {ident}")
                continue
            facts[name] = Facts(client, name, states.setdefault(ident, ClusterState()))
            label_of[name] = _cluster_label(ident, str(name))
        errors += file_duplicates(specs, lambda p: idents.get(p) if not isinstance(idents.get(p), str) else p)

        rows: list[_Row] = []
        for s in specs:
            if id(s) in unprepared or s.profile not in facts:
                continue
            try:
                row = await _state(facts[s.profile], s)
                if row.action == "create":
                    validate = validate_vm if s.kind == "vm" else validate_ct
                    errors += [f"{s.label}: {e}" for e in await validate(facts[s.profile], s)]
            except PVECliError as e:
                # An unreadable source (a 403...) is one more problem, not the end of the list
                errors.append(f"{s.label}: {e}")
                continue
            rows.append(row)
        if errors:
            # Conflicts too: the plan that would show them is not printed
            errors += [f"{r.spec.label}: conflict: {r.note}" for r in rows if r.action == "conflict"]
            # In file order: the profiles and defaults first, then entry by entry
            errors.sort(key=_file_order)
            for problem in errors:
                print_error(problem)
            print_error(f"{len(errors)} problem(s) in the file: nothing was created")
            return 1

        to_create = [r for r in rows if r.action == "create"]
        # IDs written in the file first, so the free ones handed out skip them
        for r in to_create:
            if r.spec.vmid is not None:
                facts[r.spec.profile].claim(r.spec.vmid)
        for r in to_create:
            if r.spec.vmid is None:
                r.spec.vmid = await facts[r.spec.profile].next_id()

        _print_plan(rows, label_of)
        conflicts = [r for r in rows if r.action == "conflict"]
        if conflicts:
            print_error(f"{len(conflicts)} conflict(s) with what the clusters hold: fix the file, nothing was created")
            return 1
        present = len(rows) - len(to_create)
        if not to_create:
            print_info(f"Nothing to create: the {present} guest(s) of the file are already present")
            return 0
        console.print(f"[bold]{len(to_create)} guest(s) to create[/bold]"
                      + (f", {present} already present (skipped)" if present else ""))
        if dry_run:
            print_info("Dry run: nothing was created")
            return 0
        if not yes and not confirm(f"Create these {len(to_create)} guest(s)?", default=False):
            print_cancelled()
            return 0
        results = await _execute(to_create, facts, parallel)
        return _print_summary(rows, results, label_of)


def deploy(
    file: str = typer.Argument(..., help="YAML file describing the guests"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Check everything and show the plan, create nothing"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    parallel: int = typer.Option(4, "--parallel", help="Guests created at the same time"),
    wait_ip: int = typer.Option(0, "--wait-ip", help="Seconds to wait for each guest's IPv4 address (0: don't)"),
    profile: str = typer.Option(None, "--profile", "-p", help="Profile of the guests that name none in the file"),
) -> None:
    """Deploy the VMs and containers described in a YAML file.

    The whole file is checked on every cluster before anything is written.
    A guest whose name exists on its cluster is skipped, never overwritten.
    Passwords only come from environment variables (password_env).

    Examples:
        pvecli deploy lab.yaml --dry-run
        pvecli deploy lab.yaml -y --parallel 8 --wait-ip 120
    """
    if is_multi_profile(profile):
        print_error("-p takes one profile here: put profile: on the guests of other clusters in the file")
        raise typer.Exit(1)
    try:
        doc = yaml.safe_load(Path(file).read_text())
    except OSError as e:
        print_error(f"Cannot read {file}: {e.strerror or e}")
        raise typer.Exit(1)
    except yaml.YAMLError as e:
        print_error(f"{file} is not valid YAML: {e}")
        raise typer.Exit(1)
    try:
        code = asyncio.run(_run(doc, profile, dry_run, yes, parallel, wait_ip))
    except PVECliError as e:
        print_error(str(e))
        raise typer.Exit(1)
    if code:
        raise typer.Exit(code)
